from __future__ import annotations

from typing import TYPE_CHECKING

from picopg.server.replication import ResponseSent, logger
from picopg.server.review.comments import CommentError
from picopg.server.submissions.reader import SubmissionError
from picopg.server.submissions.history import history_value

if TYPE_CHECKING:
    from picopg.server.app import PicoRequestHandler


def dispatch_submissions(handler: PicoRequestHandler, path: str) -> bool:
    if path != "/api/v1/submissions":
        return False
    try:
        if handler.command != "GET":
            handler.error(405, "method.not_allowed", "method not allowed")
            return True
        identity = handler.authenticated_replica()
        if identity is None:
            handler.error(401, "auth.required", "valid Session required")
            return True
        handler.send_json(
            200,
            history_value(
                handler.server.submissions,
                handler.server.comments,
                identity.principal_id,
            ),
            (("Cache-Control", "no-store"),),
        )
    except (CommentError, SubmissionError) as error:
        handler.error(400, "submission.invalid", str(error))
    except ResponseSent:
        pass
    except Exception:
        logger.exception("student request failed")
        handler.error(500, "server.failure", "server failure")
    return True
