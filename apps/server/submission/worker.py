from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Lock
from typing import Protocol

from apps.server.draft.protocol import decode_revision
from apps.server.log_reads import find_record, read_all
from apps.server.submission.logs import is_request_log, request_scope, result_log
from apps.server.validation import require_position, require_text
from picopg.api import Position, ReplicaLogStore, ReplicatedRecord, record_id_of


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


class SubmissionRequestConsumer:
    def __init__(
        self,
        store: ReplicaLogStore,
        request_logs: tuple[str, ...],
        tags: SubmissionTagWriter,
        on_result: Callable[[str, Position], None] | None = None,
    ) -> None:
        if len(set(request_logs)) != len(request_logs):
            raise ValueError("request_logs must be unique")
        if any(not is_request_log(log_id) for log_id in request_logs):
            raise ValueError("request_logs must contain only Submission Request Logs")
        self._store = store
        self._request_logs = set(request_logs)
        self._lock = Lock()
        self._tags = tags
        self._on_result = on_result or (lambda _log_id, _head: None)

    def run_once(self) -> int:
        with self._lock:
            request_logs = tuple(sorted(self._request_logs))
        return sum(self._run_log(log_id) for log_id in request_logs)

    def add_request_log(self, log_id: str) -> None:
        if not is_request_log(log_id):
            raise ValueError("log_id is not a Submission Request Log")
        with self._lock:
            self._request_logs.add(log_id)

    def _run_log(self, request_log: str) -> int:
        output_log = result_log(request_log)
        results = read_all(self._store, output_log)
        completed = {
            (position.log_id, position.record_id)
            for record in results
            for position in record.requires[:1]
            if position.log_id == request_log
        }
        head = None if not results else results[-1].position
        handled = 0
        for request in read_all(self._store, request_log):
            if (request_log, request.id) in completed:
                continue
            data, requires = self._result(request)
            dependencies = _unique((request.position, *requires), output_log)
            record = _record(output_log, head, dependencies, data)
            appended = self._store.append(output_log, head, (record,))
            if appended.kind != "accepted":
                raise RuntimeError(f"{output_log} lost its single writer")
            head = record.position
            self._on_result(output_log, head)
            completed.add((request_log, request.id))
            handled += 1
        return handled

    def _result(self, request: ReplicatedRecord) -> tuple[Mapping[str, object], tuple[Position, ...]]:
        try:
            return self._complete(request)
        except ValueError as error:
            return ({
                "type": "submission.failed",
                "request": request.position.to_value(),
                "message": str(error) or "Submission is invalid",
            }, ())

    def _complete(self, request: ReplicatedRecord) -> tuple[Mapping[str, object], tuple[Position, ...]]:
        principal_id, _ = request_scope(request.log_id)
        data = request.data
        if not isinstance(data, Mapping) or data.get("type") != "submission.requested":
            raise ValueError("request is not a Submission")
        submission_id = require_text(data.get("submissionId"), "submissionId")
        playground_id = require_text(data.get("playgroundId"), "playgroundId")
        revision = require_position(data.get("revision"), "revision")
        if revision not in request.requires:
            raise ValueError("Submission does not require its Revision")
        revision_record = find_record(self._store, revision)
        if revision_record is None:
            raise ValueError("Submission Revision is missing")
        decoded = decode_revision(revision_record)
        if decoded["principalId"] != principal_id:
            raise ValueError("Submission Revision belongs to another Principal")
        tag = self._tags.create(
            idempotency_key=f"{request.log_id}:{request.id}",
            submission_id=submission_id,
            playground_id=playground_id,
            revision=revision,
            source=decoded["source"],
        )
        tag = require_text(tag, "tag")
        return ({
            "type": "submission.completed",
            "submissionId": submission_id,
            "playgroundId": playground_id,
            "request": request.position.to_value(),
            "revision": revision.to_value(),
            "tag": tag,
        }, (revision,))


def _unique(positions: tuple[Position, ...], own_log: str) -> tuple[Position, ...]:
    unique: list[Position] = []
    seen: set[tuple[str, str]] = set()
    for position in positions:
        if position.log_id == own_log:
            raise ValueError("result cannot require its own Log")
        key = position.log_id, position.record_id
        if key not in seen:
            unique.append(position)
            seen.add(key)
    return tuple(unique)


def _record(
    log_id: str,
    parent: Position | None,
    requires: tuple[Position, ...],
    data: Mapping[str, object],
) -> ReplicatedRecord:
    return ReplicatedRecord(record_id_of(log_id, parent, requires, data), log_id, parent, requires, data)
