from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from picopg.server.playgrounds.releases import PLAYGROUND_RELEASES_LOG_ID
from picopg.server.replication import ResponseSent, logger

if TYPE_CHECKING:
    from picopg.server.app import PicoRequestHandler


def dispatch_lessons(handler: PicoRequestHandler, path: str) -> bool:
    if path != "/api/v1/lessons":
        return False
    try:
        if handler.command != "GET":
            handler.error(405, "method.not_allowed", "method not allowed")
            return True
        identity = handler.authenticated_replica()
        # 能读课程目录的人才能搜课程。
        if identity is None or handler.server.log_authorization is None \
                or handler.server.log_authorization.access(identity, PLAYGROUND_RELEASES_LOG_ID) != "read":
            handler.error(401, "auth.required", "valid Session required")
            return True
        query = parse_qs(urlsplit(handler.path).query)
        text = (query.get("q") or [""])[0]
        if len(text) > 200:
            handler.error(400, "lessons.query.invalid", "query is too long")
            return True
        try:
            limit = int((query.get("limit") or ["5"])[0])
        except ValueError:
            limit = 5
        handler.send_json(200, {"results": handler.server.lessons.search(text, limit)}, (("Cache-Control", "no-store"),))
    except ResponseSent:
        pass
    except Exception:
        logger.exception("lesson search failed")
        handler.error(500, "server.failure", "server failure")
    return True
