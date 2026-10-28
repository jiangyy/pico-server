from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import unquote

from picopg.server.classroom.roster import ClassRosterError
from picopg.server.replication import _ResponseSent, logger
from picopg.server.review.comments import CommentError
from picopg.server.submissions.views import detail_value, summary_value
from picopg.server.submissions.reader import SubmissionError, SubmissionNotFound
from picopg.replica import InvalidReplicaData

if TYPE_CHECKING:
    from picopg.server.app import PicoRequestHandler


def dispatch_teacher(handler: PicoRequestHandler, path: str) -> bool:
    route = _teacher_route(path)
    if route is None:
        return False
    try:
        identity = handler._authenticated_replica()
        if identity is None:
            handler._error(401, "auth.required", "valid Session required")
            return True
        action, request_record_id = route
        students = handler.server.class_rosters.directory().students_of(identity.principal_id)
        if action == "list" and handler.command == "GET":
            submissions = handler.server.submissions.list_for_principals(students)
            handler._send_json(200, {
                "submissions": [summary_value(item) for item in submissions]
            }, (("Cache-Control", "no-store"),))
            return True
        submission = handler.server.submissions.get_for(request_record_id, students)
        if action == "detail" and handler.command == "GET":
            handler._send_json(
                200,
                detail_value(handler.server.store, handler.server.comments, submission),
                (("Cache-Control", "no-store"),),
            )
            return True
        if action == "comment" and handler.command == "POST":
            body = handler._read_json()
            if not isinstance(body, dict) or set(body) != {"text"}:
                raise CommentError("comment body must contain text")
            position = handler.server.comments.add_submission(
                submission.principal_id,
                submission.playground_id,
                identity.principal_id,
                body["text"],
                submission.submission_id,
                submission.request,
                submission.revision,
            )
            handler._send_json(201, {"position": position.to_value()})
            return True
        handler._error(405, "method.not_allowed", "method not allowed")
    except SubmissionNotFound as error:
        handler._error(404, "submission.not_found", str(error))
    except (ClassRosterError, CommentError, SubmissionError, InvalidReplicaData) as error:
        handler._error(400, "submission.invalid", str(error))
    except _ResponseSent:
        pass
    except Exception:
        logger.exception("teacher request failed")
        handler._error(500, "server.failure", "server failure")
    return True


def _teacher_route(path: str) -> tuple[str, str] | None:
    if path == "/api/v1/teacher/submissions":
        return "list", ""
    parts = path.split("/")
    if parts[:5] != ["", "api", "v1", "teacher", "submissions"]:
        return None
    if len(parts) == 6:
        request_record_id = unquote(parts[5])
        return None if not request_record_id else ("detail", request_record_id)
    if len(parts) == 7 and parts[6] == "comments":
        request_record_id = unquote(parts[5])
        return None if not request_record_id else ("comment", request_record_id)
    return None
