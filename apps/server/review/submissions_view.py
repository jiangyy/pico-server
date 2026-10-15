from __future__ import annotations

from apps.server.draft.protocol import decode_revision
from apps.server.log_reads import find_record
from apps.server.review.comments import CommentPublisher
from apps.server.submission.read_model import (
    Submission,
    SubmissionError,
    SubmissionReader,
)
from picopg.api import ReplicaLogStore


def summary_value(submission: Submission) -> dict[str, object]:
    value: dict[str, object] = {
        "requestRecordId": submission.request.record_id,
        "principalId": submission.principal_id,
        "replicaId": submission.replica_id,
        "submissionId": submission.submission_id,
        "playgroundId": submission.playground_id,
        "revision": submission.revision.to_value(),
        "status": submission.status,
    }
    if submission.tag is not None:
        value["tag"] = submission.tag
    if submission.message is not None:
        value["message"] = submission.message
    return value


def detail_value(
    store: ReplicaLogStore,
    submissions: SubmissionReader,
    comments: CommentPublisher,
    request_record_id: str,
) -> dict[str, object]:
    submission = submissions.get(request_record_id)
    revision_record = find_record(store, submission.revision)
    if revision_record is None:
        raise SubmissionError("Submission Revision is missing")
    decoded = decode_revision(revision_record)
    if decoded["principalId"] != submission.principal_id:
        raise SubmissionError("Submission Revision belongs to another Principal")
    if decoded["draftId"] != submission.playground_id:
        raise SubmissionError("Submission Revision belongs to another Draft")
    value = summary_value(submission)
    value["request"] = submission.request.to_value()
    value["source"] = decoded["source"]
    value["comments"] = [
        {
            "position": comment.position.to_value(),
            "author": comment.author,
            "text": comment.text,
        }
        for comment in comments.comments(
            submission.principal_id, submission.playground_id
        )
        if comment.submission_request == submission.request
    ]
    return value
