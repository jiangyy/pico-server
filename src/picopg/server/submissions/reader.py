"""Immutable Submission facts projected from Request and Result Logs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from picopg.server.common.reads import PaginationStalled, read_all
from picopg.server.submissions.logs import is_request_log, request_scope, result_log
from picopg.server.submissions.protocol import SubmissionError, SubmissionRequest, decode_request
from picopg.server.common.validation import require_non_blank_position, require_non_blank_text
from picopg.replica import Position, ReplicaLogStore, ReplicatedRecord


class SubmissionNotFound(SubmissionError):
    pass


@dataclass(frozen=True, slots=True)
class Submission(SubmissionRequest):
    status: str
    tag: str | None = None
    message: str | None = None

class SubmissionReader:
    def __init__(self, store: ReplicaLogStore) -> None:
        self._store = store

    def list(self) -> tuple[Submission, ...]:
        return self._list(
            tuple(
                log_id
                for log_id in sorted(self._store.log_ids())
                if is_request_log(log_id)
            )
        )

    def list_for(self, principal_id: str) -> tuple[Submission, ...]:
        return self.list_for_principals((principal_id,))

    def list_for_principals(
        self, principal_ids: Iterable[str]
    ) -> tuple[Submission, ...]:
        owners = frozenset(principal_ids)
        if not owners:
            return ()
        try:
            request_logs = tuple(
                log_id
                for log_id in sorted(self._store.log_ids())
                if is_request_log(log_id)
                and request_scope(log_id)[0] in owners
            )
        except ValueError as error:
            raise SubmissionError(str(error)) from error
        return self._list(request_logs)

    def _list(self, request_logs: tuple[str, ...]) -> tuple[Submission, ...]:
        submissions: list[Submission] = []
        record_ids: set[str] = set()
        for log_id in request_logs:
            results = self._results(log_id)
            for request in self._records(log_id):
                if request.id in record_ids:
                    raise SubmissionError("submission request Record ID is not unique")
                record_ids.add(request.id)
                submissions.append(self._submission(request, results.get(request.position)))
        return tuple(submissions)

    def get(self, request_record_id: str) -> Submission:
        return self._get(request_record_id, self.list())

    def get_for(
        self, request_record_id: str, principal_ids: Iterable[str]
    ) -> Submission:
        return self._get(request_record_id, self.list_for_principals(principal_ids))

    def _get(
        self, request_record_id: str, submissions: tuple[Submission, ...]
    ) -> Submission:
        matches = [
            item for item in submissions if item.request.record_id == request_record_id
        ]
        if not matches:
            raise SubmissionNotFound("submission not found")
        if len(matches) != 1:
            raise SubmissionError("submission request Record ID is ambiguous")
        return matches[0]

    def _results(self, request_log: str) -> dict[Position, ReplicatedRecord]:
        results: dict[Position, ReplicatedRecord] = {}
        for record in self._records(result_log(request_log)):
            request = _result_request(record)
            if request.log_id != request_log:
                raise SubmissionError("Submission result belongs to another Request Log")
            if request in results:
                raise SubmissionError("Submission has more than one result")
            results[request] = record
        return results

    def _records(self, log_id: str) -> tuple[ReplicatedRecord, ...]:
        try:
            return read_all(self._store, log_id)
        except PaginationStalled as error:
            raise SubmissionError(str(error)) from error

    def _submission(
        self, request: ReplicatedRecord, result: ReplicatedRecord | None
    ) -> Submission:
        decoded = decode_request(request)
        principal_id, replica_id = decoded.principal_id, decoded.replica_id
        submission_id, playground_id = decoded.submission_id, decoded.playground_id
        revision = decoded.revision
        if result is None:
            return Submission(
                request.position, principal_id, replica_id, submission_id,
                playground_id, revision, "requested",
            )
        result_data = result.data
        if not isinstance(result_data, Mapping):
            raise SubmissionError("Submission result must be an object")
        kind = result_data.get("type")
        if kind == "submission.failed":
            return Submission(
                request.position, principal_id, replica_id, submission_id,
                playground_id, revision, "failed",
                message=require_non_blank_text(
                    result_data.get("message"),
                    "message",
                    error_type=SubmissionError,
                ),
            )
        if kind != "submission.completed":
            raise SubmissionError("Submission result type is invalid")
        if result_data.get("submissionId") != submission_id:
            raise SubmissionError("Submission result names another Submission")
        if result_data.get("playgroundId") != playground_id:
            raise SubmissionError("Submission result names another Playground")
        if require_non_blank_position(
            result_data.get("revision"),
            "revision",
            error_type=SubmissionError,
        ) != revision:
            raise SubmissionError("Submission result names another Revision")
        return Submission(
            request.position, principal_id, replica_id, submission_id,
            playground_id, revision, "completed",
            tag=require_non_blank_text(
                result_data.get("tag"),
                "tag",
                error_type=SubmissionError,
            ),
        )


def _result_request(record: ReplicatedRecord) -> Position:
    data = record.data
    if not isinstance(data, Mapping):
        raise SubmissionError("Submission result must be an object")
    request = require_non_blank_position(
        data.get("request"),
        "request",
        error_type=SubmissionError,
    )
    if not record.requires or record.requires[0] != request:
        raise SubmissionError("Submission result does not require its Request")
    return request
