from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Lock

from picopg.server.drafts.authority import DraftAuthority
from picopg.server.drafts.protocol import DraftResult
from picopg.server.common.reads import read_all
from picopg.replica import Position, ReplicaLogStore


class DraftRequestConsumer:
    def __init__(
        self,
        store: ReplicaLogStore,
        request_logs: tuple[str, ...],
        on_decision: Callable[[str, Position], None] | None = None,
    ) -> None:
        if len(set(request_logs)) != len(request_logs):
            raise ValueError("request_logs must be unique")
        self._store = store
        self._request_logs = set(request_logs)
        self._lock = Lock()
        self._authority = DraftAuthority(store)
        self._on_decision = on_decision or (lambda _log_id, _head: None)
        self._processed: set[tuple[str, str]] = set()

    def run_once(self) -> tuple[tuple[Position, DraftResult], ...]:
        outcomes: list[tuple[Position, DraftResult]] = []
        with self._lock:
            request_logs = tuple(sorted(self._request_logs))
        for log_id in request_logs:
            for record in read_all(self._store, log_id):
                key = (log_id, record.id)
                if key in self._processed:
                    continue
                result = self._authority.handle(record.position)
                if result.kind != "missingDependency":
                    self._processed.add(key)
                outcomes.append((record.position, result))
                if result.decision is not None:
                    self._on_decision(result.decision.log_id, result.decision)
        return tuple(outcomes)

    def add_request_log(self, log_id: str) -> None:
        if not is_draft_request_log(log_id):
            raise ValueError("log_id is not a Draft Request Log")
        with self._lock:
            self._request_logs.add(log_id)


def draft_request_logs(bindings: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(sorted(
        log_id for log_id, access in bindings.items()
        if access == "append" and is_draft_request_log(log_id)
    ))


def is_draft_request_log(log_id: str) -> bool:
    parts = log_id.split("/")
    return (
        len(parts) == 6
        and parts[0] == "principal"
        and bool(parts[1])
        and parts[2] == "draft"
        and bool(parts[3])
        and parts[4] == "request"
        and bool(parts[5])
    )
