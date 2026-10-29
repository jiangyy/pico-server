"""Client diagnostics: browser warnings and errors, kept apart from learning evidence."""

from __future__ import annotations

import logging
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

from picopg.server.replication import ResponseSent

if TYPE_CHECKING:
    from picopg.server.app import PicoRequestHandler

client_logger = logging.getLogger("picopg.client")

LEVELS = frozenset({"warn", "error", "uncaught", "unhandled", "csp"})
MAX_TEXT = 8_000
MAX_PAGE = 300
PER_MINUTE = 30


class ClientLogLimiter:
    """Per-replica budget so one broken page cannot flood the log."""

    def __init__(self, per_minute: int = PER_MINUTE, clock=time.monotonic) -> None:
        self.per_minute = per_minute
        self.clock = clock
        self.windows: dict[str, tuple[float, int]] = {}
        self.lock = threading.Lock()

    def allow(self, replica: str) -> bool:
        now = self.clock()
        with self.lock:
            start, count = self.windows.get(replica, (now, 0))
            if now - start >= 60:
                start, count = now, 0
            if count >= self.per_minute:
                return False
            self.windows[replica] = (start, count + 1)
            return True


def write_client_log_to(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    client_logger.addHandler(handler)


def dispatch_client_log(handler: PicoRequestHandler, path: str) -> bool:
    if path != "/api/v1/client-log":
        return False
    try:
        if handler.command != "POST":
            handler.error(405, "method.not_allowed", "method not allowed")
            return True
        replica = handler.authenticated_replica()
        if replica is None:
            handler.error(401, "auth.required", "valid Session required")
            return True
        level, page, text = _entry(handler.read_json())
        if not handler.server.client_log_limiter.allow(f"{replica.principal_id}/{replica.replica_id}"):
            handler.error(429, "client_log.limited", "too many client log entries")
            return True
        client_logger.warning(
            "[%s] %s/%s %s\n%s", level, replica.principal_id, replica.replica_id, page, text,
        )
        handler.send_json(200, {"logged": True}, (("Cache-Control", "no-store"),))
    except ValueError as error:
        handler.error(400, "client_log.invalid", str(error))
    except ResponseSent:
        pass
    return True


def _entry(value: object) -> tuple[str, str, str]:
    if not isinstance(value, dict) or set(value) != {"level", "page", "text"}:
        raise ValueError("body must contain level, page and text")
    level, page, text = value["level"], value["page"], value["text"]
    if level not in LEVELS:
        raise ValueError("level is invalid")
    if not isinstance(page, str) or len(page) > MAX_PAGE:
        raise ValueError("page is invalid")
    if not isinstance(text, str):
        raise ValueError("text is invalid")
    return level, page, text[:MAX_TEXT]
