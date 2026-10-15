from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import unquote

from apps.server.classroom.roster import ClassRosterError
from apps.server.protocol import _ResponseSent, logger
from picopg.api import InvalidReplicaData

if TYPE_CHECKING:
    from apps.server.application import PicoRequestHandler


def dispatch_classroom(handler: PicoRequestHandler, path: str) -> bool:
    class_id = _class_route(path)
    if class_id is None:
        return False
    try:
        if not handler._admin_authorized():
            handler._error(401, "auth.required", "valid operator token required")
            return True
        if handler.command != "POST":
            handler._error(405, "method.not_allowed", "method not allowed")
            return True
        body = handler._read_json()
        if not isinstance(body, dict) or set(body) != {"teachers", "students"}:
            raise ClassRosterError("body must contain teachers and students")
        position = handler.server.class_rosters.replace(
            class_id,
            body["teachers"],
            body["students"],
        )
        handler._send_json(201, {"position": position.to_value()})
    except (ClassRosterError, InvalidReplicaData) as error:
        handler._error(400, "class.invalid", str(error))
    except _ResponseSent:
        pass
    except Exception:
        logger.exception("class roster request failed")
        handler._error(500, "server.failure", "server failure")
    return True


def _class_route(path: str) -> str | None:
    parts = path.split("/")
    if parts[:5] != ["", "api", "v1", "operator", "classes"] or len(parts) != 6:
        return None
    class_id = unquote(parts[5])
    return class_id or None
