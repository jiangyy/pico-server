from __future__ import annotations

from typing import TYPE_CHECKING

from apps.server.protocol import _ResponseSent, logger
from apps.server.review.comments import CommentError
from apps.server.submission.read_model import SubmissionError
from apps.server.student.submissions import history_value

if TYPE_CHECKING:
    from apps.server.application import PicoRequestHandler


def dispatch_student(handler: PicoRequestHandler, path: str) -> bool:
    if path != "/api/v1/submissions":
        return False
    try:
        if handler.command != "GET":
            handler._error(405, "method.not_allowed", "method not allowed")
            return True
        identity = handler._authenticated_replica()
        if identity is None:
            handler._error(401, "auth.required", "valid Session required")
            return True
        handler._send_json(
            200,
            history_value(
                handler.server.submissions,
                handler.server.comments,
                identity.principal_id,
            ),
            (("Cache-Control", "no-store"),),
        )
    except (CommentError, SubmissionError) as error:
        handler._error(400, "submission.invalid", str(error))
    except _ResponseSent:
        pass
    except Exception:
        logger.exception("student request failed")
        handler._error(500, "server.failure", "server failure")
    return True
