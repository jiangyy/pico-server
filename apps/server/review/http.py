from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import unquote

from apps.server.protocol import _ResponseSent, logger
from apps.server.review.comments import CommentError
from apps.server.submission.read_model import SubmissionError, SubmissionNotFound
from picopg.api import InvalidReplicaData

if TYPE_CHECKING:
    from apps.server.application import PicoRequestHandler


def dispatch_review(handler: PicoRequestHandler, path: str) -> bool:
    submission = _submission_route(path)
    comment = _comment_route(path)
    if submission is None and comment is None:
        return False
    try:
        if not handler._admin_authorized():
            handler._error(401, "auth.required", "valid operator token required")
            return True
        if submission is not None:
            _submission(handler, *submission)
        else:
            _comment(handler, *comment)
    except SubmissionNotFound as error:
        handler._error(404, "submission.not_found", str(error))
    except (CommentError, SubmissionError, InvalidReplicaData) as error:
        handler._error(
            400,
            "submission.invalid" if submission is not None else "comment.invalid",
            str(error),
        )
    except _ResponseSent:
        pass
    except Exception:
        logger.exception("review request failed")
        handler._error(500, "server.failure", "server failure")
    return True

def _submission(handler: PicoRequestHandler, action: str, request_record_id: str) -> None:
    if action == "list" and handler.command == "GET":
        handler._send_json(200, {
            "submissions": [item.summary_value() for item in handler.server.submissions.list()]
        }, (("Cache-Control", "no-store"),))
        return
    if action == "detail" and handler.command == "GET":
        handler._send_json(
            200,
            handler.server.submissions.detail_value(request_record_id),
            (("Cache-Control", "no-store"),),
        )
        return
    if action == "comment" and handler.command == "POST":
        body = handler._read_json()
        if not isinstance(body, dict) or set(body) != {"author", "text"}:
            raise CommentError("comment body must contain author and text")
        submission = handler.server.submissions.get(request_record_id)
        position = handler.server.comments.add_submission(
            submission.principal_id,
            submission.playground_id,
            body["author"],
            body["text"],
            submission.submission_id,
            submission.request,
            submission.revision,
        )
        handler._send_json(201, {"position": position.to_value()})
        return
    handler._error(405, "method.not_allowed", "method not allowed")


def _comment(handler: PicoRequestHandler, principal_id: str, draft_id: str) -> None:
    if handler.command != "POST":
        handler._error(405, "method.not_allowed", "method not allowed")
        return
    body = handler._read_json()
    if not isinstance(body, dict) or set(body) != {"author", "text"}:
        raise CommentError("comment body must contain author and text")
    position = handler.server.comments.add(principal_id, draft_id, body["author"], body["text"])
    handler._send_json(201, {"position": position.to_value()})


def _comment_route(path: str) -> tuple[str, str] | None:
    parts = path.split("/")
    if (
        parts[:4] != ["", "api", "v1", "principals"]
        or len(parts) != 8
        or parts[5] != "drafts"
        or parts[7] != "comments"
    ):
        return None
    principal_id = unquote(parts[4])
    draft_id = unquote(parts[6])
    return (principal_id, draft_id) if principal_id and draft_id else None


def _submission_route(path: str) -> tuple[str, str] | None:
    if path == "/api/v1/operator/submissions":
        return "list", ""
    parts = path.split("/")
    if parts[:5] != ["", "api", "v1", "operator", "submissions"]:
        return None
    if len(parts) == 6:
        request_record_id = unquote(parts[5])
        return None if not request_record_id else ("detail", request_record_id)
    if len(parts) == 7 and parts[6] == "comments":
        request_record_id = unquote(parts[5])
        return None if not request_record_id else ("comment", request_record_id)
    return None
