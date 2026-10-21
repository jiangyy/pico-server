from __future__ import annotations

from apps.server.review.comments import CommentPublisher
from apps.server.submission.read_model import Submission
from apps.server.submission.protocol import read_source
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
    comments: CommentPublisher,
    submission: Submission,
) -> dict[str, object]:
    source = read_source(store, submission)
    value = summary_value(submission)
    value["request"] = submission.request.to_value()
    value["source"] = source
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
