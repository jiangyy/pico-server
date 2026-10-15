"""Operator projection over immutable Submission and Revision facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from apps.server.draft.protocol import decode_revision
from apps.server.log_reads import PaginationStalled, find_record, read_all
from apps.server.review.comments import CommentPublisher
from apps.server.submission.logs import is_request_log, request_scope, result_log
from apps.server.validation import require_non_blank_position, require_non_blank_text
from picopg.api import Position, ReplicaLogStore, ReplicatedRecord


class SubmissionError(ValueError):
    pass


class SubmissionNotFound(SubmissionError):
    pass


@dataclass(frozen=True, slots=True)
class Submission:
    request: Position
    principal_id: str
    replica_id: str
    submission_id: str
    playground_id: str
    revision: Position
    status: str
    tag: str | None = None
    message: str | None = None

    def summary_value(self) -> dict[str, object]:
        value: dict[str, object] = {
            "requestRecordId": self.request.record_id,
            "principalId": self.principal_id,
            "replicaId": self.replica_id,
            "submissionId": self.submission_id,
            "playgroundId": self.playground_id,
            "revision": self.revision.to_value(),
            "status": self.status,
        }
        if self.tag is not None:
            value["tag"] = self.tag
        if self.message is not None:
            value["message"] = self.message
        return value


class SubmissionReader:
    def __init__(self, store: ReplicaLogStore, comments: CommentPublisher) -> None:
        self._store = store
        self._comments = comments

    def list(self) -> tuple[Submission, ...]:
        return self._list(
            tuple(
                log_id
                for log_id in sorted(self._store.log_ids())
                if is_request_log(log_id)
            )
        )

    def list_for(self, principal_id: str) -> tuple[Submission, ...]:
        try:
            request_logs = tuple(
                log_id
                for log_id in sorted(self._store.log_ids())
                if is_request_log(log_id)
                and request_scope(log_id)[0] == principal_id
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
        matches = [item for item in self.list() if item.request.record_id == request_record_id]
        if not matches:
            raise SubmissionNotFound("submission not found")
        if len(matches) != 1:
            raise SubmissionError("submission request Record ID is ambiguous")
        return matches[0]

    def detail_value(self, request_record_id: str) -> dict[str, object]:
        submission = self.get(request_record_id)
        revision_record = find_record(self._store, submission.revision)
        if revision_record is None:
            raise SubmissionError("Submission Revision is missing")
        decoded = decode_revision(revision_record)
        if decoded["principalId"] != submission.principal_id:
            raise SubmissionError("Submission Revision belongs to another Principal")
        if decoded["draftId"] != submission.playground_id:
            raise SubmissionError("Submission Revision belongs to another Draft")
        value = submission.summary_value()
        value["request"] = submission.request.to_value()
        value["source"] = decoded["source"]
        value["comments"] = [
            {
                "position": comment.position.to_value(),
                "author": comment.author,
                "text": comment.text,
            }
            for comment in self._comments.comments(
                submission.principal_id, submission.playground_id
            )
            if comment.submission_request == submission.request
        ]
        return value

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
        principal_id, replica_id = request_scope(request.log_id)
        data = request.data
        if not isinstance(data, Mapping) or data.get("type") != "submission.requested":
            raise SubmissionError("request is not a Submission")
        submission_id = require_non_blank_text(
            data.get("submissionId"),
            "submissionId",
            error_type=SubmissionError,
        )
        playground_id = require_non_blank_text(
            data.get("playgroundId"),
            "playgroundId",
            error_type=SubmissionError,
        )
        revision = require_non_blank_position(
            data.get("revision"),
            "revision",
            error_type=SubmissionError,
        )
        if revision not in request.requires:
            raise SubmissionError("Submission does not require its Revision")
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
