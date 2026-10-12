"""Server-side consumers for Request Logs."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Event, Thread
from typing import Protocol

from picopg.api import Position, ReplicaLogStore, ReplicatedRecord, record_id_of

from apps.server.drafts import DraftAuthority, DraftResult

logger = logging.getLogger("picopg.server.worker")


class RequestHandler(Protocol):
    def __call__(self, request: ReplicatedRecord, position: Position) -> "WorkerResult | None": ...


class LogConsumer(Protocol):
    def run_once(self) -> object: ...


@dataclass(frozen=True, slots=True)
class WorkerResult:
    data: Mapping[str, object]
    requires: tuple[Position, ...] = ()


class SubmissionTagWriter(Protocol):
    def create(
        self,
        *,
        idempotency_key: str,
        submission_id: str,
        playground_id: str,
        revision: Position,
        source: str,
    ) -> str: ...


class SubmissionHandler:
    def __init__(self, store: ReplicaLogStore, tags: SubmissionTagWriter) -> None:
        self._store = store
        self._tags = tags

    def __call__(self, record: ReplicatedRecord, request: Position) -> WorkerResult:
        data = record.data
        if not isinstance(data, Mapping) or data.get("type") != "submission.requested":
            raise ValueError("request is not a Submission")
        submission_id = _non_empty(data.get("submissionId"), "submissionId")
        playground_id = _non_empty(data.get("playgroundId"), "playgroundId")
        revision = _position(data.get("revision"), "revision")
        if revision not in record.requires:
            raise ValueError("Submission does not require its Revision")
        revision_record = _at(self._store, revision)
        if revision_record is None:
            raise ValueError("Submission Revision is missing")
        from apps.server.drafts import _decode_revision

        source = _decode_revision(revision_record)["source"]
        tag = self._tags.create(
            idempotency_key=f"{request.log_id}:{request.record_id}",
            submission_id=submission_id,
            playground_id=playground_id,
            revision=revision,
            source=source,
        )
        if not isinstance(tag, str) or not tag:
            raise ValueError("tag must be non-empty")
        return WorkerResult(
            {
                "type": "submission.completed",
                "submissionId": submission_id,
                "playgroundId": playground_id,
                "request": request.to_value(),
                "revision": revision.to_value(),
                "tag": tag,
            },
            (revision,),
        )


class LogWorker:
    """Runs idempotent work from request Logs and records results in a result Log."""

    def __init__(
        self,
        store: ReplicaLogStore,
        request_logs: tuple[str, ...],
        result_log: str,
        handle: RequestHandler,
        on_result: Callable[[str, Position], None] | None = None,
    ) -> None:
        if not request_logs or len(set(request_logs)) != len(request_logs):
            raise ValueError("request_logs must be non-empty and unique")
        if not result_log or result_log in request_logs:
            raise ValueError("result_log must be non-empty and separate from request Logs")
        self._store = store
        self._request_logs = request_logs
        self._result_log = result_log
        self._handle = handle
        self._on_result = on_result or (lambda _log_id, _head: None)

    def run_once(self) -> int:
        results = _records(self._store, self._result_log)
        request_ids = set(self._request_logs)
        completed = {
            (position.log_id, position.record_id)
            for record in results
            for position in record.requires[:1]
            if position.log_id in request_ids
        }
        result_head = None if not results else results[-1].position
        handled = 0
        for log_id in self._request_logs:
            for request in _records(self._store, log_id):
                request_position = request.position
                if (log_id, request.id) in completed:
                    continue
                result = self._handle(request, request_position)
                if result is None:
                    continue
                dependencies: list[Position] = []
                seen: set[tuple[str, str]] = set()
                for required in (request_position, *result.requires):
                    if required.log_id == self._result_log:
                        raise ValueError("result cannot require its own Log")
                    key = (required.log_id, required.record_id)
                    if key not in seen:
                        dependencies.append(required)
                        seen.add(key)
                record = _record(self._result_log, result_head, tuple(dependencies), result.data)
                appended = self._store.append(self._result_log, result_head, (record,))
                if appended.kind != "accepted":
                    raise RuntimeError(f"{self._result_log} lost its single writer")
                result_head = record.position
                self._on_result(self._result_log, result_head)
                completed.add((log_id, request.id))
                handled += 1
        return handled


class DraftRequestConsumer:
    """Applies Draft commands and appends accepted decisions to the Decision Log."""

    def __init__(
        self,
        store: ReplicaLogStore,
        request_logs: tuple[str, ...],
        on_decision: Callable[[str, Position], None] | None = None,
    ) -> None:
        if not request_logs or len(set(request_logs)) != len(request_logs):
            raise ValueError("request_logs must be non-empty and unique")
        self._store = store
        self._request_logs = request_logs
        self._authority = DraftAuthority(store)
        self._on_decision = on_decision or (lambda _log_id, _head: None)
        self._processed: set[tuple[str, str]] = set()

    def run_once(self) -> tuple[tuple[Position, DraftResult], ...]:
        outcomes: list[tuple[Position, DraftResult]] = []
        for log_id in self._request_logs:
            for record in _records(self._store, log_id):
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


class ServerWorkerThread:
    """Wakes log consumers after appends; scans configured request Logs on startup."""

    def __init__(self, consumers: tuple[LogConsumer, ...]) -> None:
        self._consumers = consumers
        self._wake = Event()
        self._stop = Event()
        self._thread = Thread(target=self._run, name="picopg-log-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._wake.set()

    def notify_append(self, _log_id: str) -> None:
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                return
            failed = False
            for consumer in self._consumers:
                try:
                    consumer.run_once()
                except Exception:
                    logger.exception("request Log consumer failed")
                    failed = True
            if failed and not self._stop.is_set():
                self._stop.wait(1)
                self._wake.set()


def draft_request_logs(bindings: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(sorted(
        log_id for log_id, access in bindings.items()
        if access == "append" and _is_draft_request_log(log_id)
    ))


def submission_request_logs(bindings: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(sorted(
        log_id for log_id, access in bindings.items()
        if access == "append" and _is_submission_request_log(log_id)
    ))


def submission_result_log(request_log: str) -> str:
    parts = request_log.split("/")
    if len(parts) != 3 or parts[0] != "submission" or not parts[1] or parts[2] != "requests":
        raise ValueError("request_log is not a Submission Request Log")
    return f"submission/{parts[1]}/results"


def _is_draft_request_log(log_id: str) -> bool:
    parts = log_id.split("/")
    return len(parts) == 4 and parts[0] == "draft" and bool(parts[1]) and parts[2] == "request" and bool(parts[3])


def _is_submission_request_log(log_id: str) -> bool:
    parts = log_id.split("/")
    return len(parts) == 3 and parts[0] == "submission" and bool(parts[1]) and parts[2] == "requests"


def _records(store: ReplicaLogStore, log_id: str) -> tuple[ReplicatedRecord, ...]:
    values: list[ReplicatedRecord] = []
    after: Position | None = None
    while True:
        page = store.read(log_id, after, 128)
        values.extend(page.records)
        if not page.has_more:
            return tuple(values)
        if not page.records:
            raise ValueError(f"{log_id} returned an empty continuation page")
        after = page.records[-1].position


def _record(
    log_id: str,
    parent: Position | None,
    requires: tuple[Position, ...],
    data: Mapping[str, object],
) -> ReplicatedRecord:
    return ReplicatedRecord(record_id_of(log_id, parent, requires, data), log_id, parent, requires, data)


def _position(value: object, label: str) -> Position:
    if not isinstance(value, Mapping) or set(value) != {"logId", "recordId"}:
        raise ValueError(f"{label} must contain logId and recordId")
    return Position(_non_empty(value["logId"], f"{label}.logId"), _non_empty(value["recordId"], f"{label}.recordId"))


def _at(store: ReplicaLogStore, position: Position) -> ReplicatedRecord | None:
    if not store.has(position):
        return None
    after: Position | None = None
    while True:
        page = store.read(position.log_id, after, 128)
        found = next((record for record in page.records if record.id == position.record_id), None)
        if found is not None or not page.has_more:
            return found
        if not page.records:
            raise ValueError(f"{position.log_id} returned an empty continuation page")
        after = page.records[-1].position


def _non_empty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty")
    return value
