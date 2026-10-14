"""Operator projection over immutable Submission and Revision facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from apps.server.review.comments import CommentPublisher
from apps.server.draft.protocol import decode_revision
from apps.server.submission.logs import is_request_log, request_scope, result_log
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
        submissions: list[Submission] = []
        record_ids: set[str] = set()
        for log_id in sorted(self._store.log_ids()):
            if not is_request_log(log_id):
                continue
            results = self._results(log_id)
            for request in _records(self._store, log_id):
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
        revision_record = _at(self._store, submission.revision)
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
        for record in _records(self._store, result_log(request_log)):
            request = _result_request(record)
            if request.log_id != request_log:
                raise SubmissionError("Submission result belongs to another Request Log")
            if request in results:
                raise SubmissionError("Submission has more than one result")
            results[request] = record
        return results

    def _submission(
        self, request: ReplicatedRecord, result: ReplicatedRecord | None
    ) -> Submission:
        principal_id, replica_id = request_scope(request.log_id)
        data = request.data
        if not isinstance(data, Mapping) or data.get("type") != "submission.requested":
            raise SubmissionError("request is not a Submission")
        submission_id = _text(data.get("submissionId"), "submissionId")
        playground_id = _text(data.get("playgroundId"), "playgroundId")
        revision = _position(data.get("revision"), "revision")
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
                message=_text(result_data.get("message"), "message"),
            )
        if kind != "submission.completed":
            raise SubmissionError("Submission result type is invalid")
        if result_data.get("submissionId") != submission_id:
            raise SubmissionError("Submission result names another Submission")
        if result_data.get("playgroundId") != playground_id:
            raise SubmissionError("Submission result names another Playground")
        if _position(result_data.get("revision"), "revision") != revision:
            raise SubmissionError("Submission result names another Revision")
        return Submission(
            request.position, principal_id, replica_id, submission_id,
            playground_id, revision, "completed",
            tag=_text(result_data.get("tag"), "tag"),
        )


def _result_request(record: ReplicatedRecord) -> Position:
    data = record.data
    if not isinstance(data, Mapping):
        raise SubmissionError("Submission result must be an object")
    request = _position(data.get("request"), "request")
    if not record.requires or record.requires[0] != request:
        raise SubmissionError("Submission result does not require its Request")
    return request


def _records(store: ReplicaLogStore, log_id: str) -> tuple[ReplicatedRecord, ...]:
    values: list[ReplicatedRecord] = []
    after: Position | None = None
    while True:
        page = store.read(log_id, after, 128)
        values.extend(page.records)
        if not page.has_more:
            return tuple(values)
        if not page.records:
            raise SubmissionError(f"{log_id} returned an empty continuation page")
        after = page.records[-1].position


def _at(store: ReplicaLogStore, position: Position) -> ReplicatedRecord | None:
    if not store.has(position):
        return None
    return next(
        (record for record in _records(store, position.log_id) if record.id == position.record_id),
        None,
    )


def _position(value: object, label: str) -> Position:
    if not isinstance(value, Mapping) or set(value) != {"logId", "recordId"}:
        raise SubmissionError(f"{label} must contain logId and recordId")
    return Position(
        _text(value["logId"], f"{label}.logId"),
        _text(value["recordId"], f"{label}.recordId"),
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SubmissionError(f"{label} must be non-empty")
    return value
