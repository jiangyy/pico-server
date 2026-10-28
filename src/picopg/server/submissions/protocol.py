"""Submission identity and the ownership of its pinned Revision."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from picopg.server.drafts.protocol import decode_revision
from picopg.server.common.reads import find_record
from picopg.server.submissions.logs import request_scope
from picopg.server.common.validation import require_non_blank_position, require_non_blank_text
from picopg.replica import Position, ReplicaLogStore, ReplicatedRecord


class SubmissionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SubmissionRequest:
    request: Position
    principal_id: str
    replica_id: str
    submission_id: str
    playground_id: str
    revision: Position


def decode_request(record: ReplicatedRecord) -> SubmissionRequest:
    try:
        principal_id, replica_id = request_scope(record.log_id)
    except ValueError as error:
        raise SubmissionError("Submission request Log is invalid") from error
    data = record.data
    if not isinstance(data, Mapping) or data.get("type") != "submission.requested":
        raise SubmissionError("request is not a Submission")
    submission_id = require_non_blank_text(
        data.get("submissionId"), "submissionId", error_type=SubmissionError
    )
    playground_id = require_non_blank_text(
        data.get("playgroundId"), "playgroundId", error_type=SubmissionError
    )
    revision = require_non_blank_position(
        data.get("revision"), "revision", error_type=SubmissionError
    )
    if revision not in record.requires:
        raise SubmissionError("Submission does not require its Revision")
    return SubmissionRequest(
        record.position, principal_id, replica_id, submission_id, playground_id, revision
    )


def source_from_revision(request: SubmissionRequest, record: ReplicatedRecord) -> str:
    if record.position != request.revision:
        raise SubmissionError("Submission does not reference this Revision")
    try:
        revision = decode_revision(record)
    except ValueError as error:
        raise SubmissionError("Submission Revision is invalid") from error
    if revision["principalId"] != request.principal_id:
        raise SubmissionError("Submission Revision belongs to another Principal")
    if revision["draftId"] != request.playground_id:
        raise SubmissionError("Submission Revision belongs to another Draft")
    return revision["source"]


def read_source(store: ReplicaLogStore, request: SubmissionRequest) -> str:
    record = find_record(store, request.revision)
    if record is None:
        raise SubmissionError("Submission Revision is missing")
    return source_from_revision(request, record)
