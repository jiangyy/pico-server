from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Lock

from picopg.server.drafts.authority import DraftAuthority
from picopg.server.drafts.protocol import DraftResult
from picopg.server.common.reads import read_after
from picopg.server.common.workers import Retry
from picopg.replica import Position, ReplicaLogStore


logger = logging.getLogger("picopg.server.worker")


class DraftRequestConsumer:
    """每个请求日志记着读到哪里，醒来只读新追加的请求；依赖还没到的请求留着，下次醒来再试。

    游标写进 checkpoint 文件：重启后从上次处理到的地方接着做，不把历史请求在新的状态下重放一遍
    （重放一条当时结果是 current 的 beginEditing，会对早已结束的命令追加一条多余的拒绝）。
    一个日志出错只退避它自己，别的学生照常处理。
    """

    def __init__(
        self,
        store: ReplicaLogStore,
        request_logs: tuple[str, ...],
        on_decision: Callable[[str, Position], None] | None = None,
        checkpoint: Path | None = None,
    ) -> None:
        if len(set(request_logs)) != len(request_logs):
            raise ValueError("request_logs must be unique")
        self._store = store
        self._request_logs = set(request_logs)
        self._lock = Lock()
        self._authority = DraftAuthority(store)
        self._on_decision = on_decision or (lambda _log_id, _head: None)
        self._checkpoint = checkpoint
        self._cursors: dict[str, Position] = self._load_checkpoint()
        self._waiting: dict[Position, None] = {}
        self._retry = Retry()

    def run_once(self) -> tuple[tuple[Position, DraftResult], ...]:
        outcomes: list[tuple[Position, DraftResult]] = []
        with self._lock:
            request_logs = tuple(sorted(self._request_logs))
        waiting = tuple(self._waiting)
        self._waiting.clear()
        for position in waiting:
            outcomes.append(self._handle(position))
        moved = False
        for log_id in request_logs:
            if not self._retry.ready(log_id):
                continue
            try:
                for record in read_after(self._store, log_id, self._cursors.get(log_id)):
                    outcomes.append(self._handle(record.position))
                    self._cursors[log_id] = record.position
                    moved = True
            except Exception as error:
                self._retry.failed(log_id, error)
            else:
                self._retry.succeeded(log_id)
        if moved:
            self._save_checkpoint()
        return tuple(outcomes)

    def next_wake_delay(self) -> float | None:
        return self._retry.next_delay()

    def _handle(self, position: Position) -> tuple[Position, DraftResult]:
        result = self._authority.handle(position)
        if result.kind == "missingDependency":
            self._waiting[position] = None
        if result.decision is not None:
            self._on_decision(result.decision.log_id, result.decision)
        return position, result

    def _load_checkpoint(self) -> dict[str, Position]:
        if self._checkpoint is None or not self._checkpoint.exists():
            return {}
        try:
            value = json.loads(self._checkpoint.read_text(encoding="utf-8"))
            cursors = {log_id: Position.from_value(position, log_id=log_id) for log_id, position in value.items()}
        except (OSError, ValueError, AttributeError) as error:
            # 读不出就从头处理：最坏是重放一遍，不会丢请求。
            logger.warning("ignoring unreadable draft worker checkpoint %s: %s", self._checkpoint, error)
            return {}
        # 只认 Store 里还在的位置（例如换了一份数据目录）。
        return {log_id: position for log_id, position in cursors.items() if self._store.has(position)}

    def _save_checkpoint(self) -> None:
        if self._checkpoint is None:
            return
        try:
            self._checkpoint.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._checkpoint.with_suffix(".tmp")
            temporary.write_text(json.dumps({log_id: position.to_value() for log_id, position in self._cursors.items()}), encoding="utf-8")
            os.replace(temporary, self._checkpoint)
        except OSError as error:
            logger.warning("cannot write draft worker checkpoint %s: %s", self._checkpoint, error)

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
