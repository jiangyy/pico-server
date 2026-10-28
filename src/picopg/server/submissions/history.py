from __future__ import annotations

from picopg.server.review.comments import CommentPublisher
from picopg.server.submissions.reader import SubmissionReader


def history_value(
    submissions: SubmissionReader,
    comments: CommentPublisher,
    principal_id: str,
) -> dict[str, object]:
    comments_by_draft = {}
    values = []
    for submission in submissions.list_for(principal_id):
        draft_comments = comments_by_draft.setdefault(
            submission.playground_id,
            comments.comments(submission.principal_id, submission.playground_id),
        )
        value = {
            "id": submission.request.record_id,
            "submissionId": submission.submission_id,
            "playgroundId": submission.playground_id,
            "status": submission.status,
            "comments": [
                {"author": comment.author, "text": comment.text}
                for comment in draft_comments
                if comment.submission_request == submission.request
            ],
        }
        if submission.tag is not None:
            value["tag"] = submission.tag
        if submission.message is not None:
            value["message"] = submission.message
        values.append(value)
    return {"submissions": values}
