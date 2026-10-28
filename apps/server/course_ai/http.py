from __future__ import annotations

import re
from typing import TYPE_CHECKING

from apps.server.course_ai.agent import agent_request
from apps.server.course_ai.service import CourseAiError
from apps.server.protocol import _ResponseSent, logger

if TYPE_CHECKING:
    from apps.server.application import PicoRequestHandler


def dispatch_course_ai(handler: PicoRequestHandler, path: str) -> bool:
    if path == "/api/v1/ai/agent":
        return _dispatch_agent(handler)
    if path != "/api/v1/ai/course":
        return False
    try:
        if handler.command != "POST":
            handler._error(405, "method.not_allowed", "method not allowed")
            return True
        if handler._authenticated_replica() is None:
            handler._error(401, "auth.required", "valid Session required")
            return True
        if handler.server.course_ai is None:
            handler._error(503, "ai.unavailable", "course AI is not configured")
            return True
        playground_id, version_id, request = _request_value(handler._read_json())
        reply = handler.server.course_ai.reply(playground_id, version_id, request)
        handler._send_json(200, reply, (("Cache-Control", "no-store"),))
    except ValueError as error:
        handler._error(400, "ai.request.invalid", str(error))
    except CourseAiError as error:
        handler._error(502, "ai.reply.invalid", str(error))
    except _ResponseSent:
        pass
    except Exception:
        logger.exception("course AI request failed")
        handler._error(500, "server.failure", "server failure")
    return True


def _dispatch_agent(handler: PicoRequestHandler) -> bool:
    try:
        if handler.command != "POST":
            handler._error(405, "method.not_allowed", "method not allowed")
            return True
        if handler._authenticated_replica() is None:
            handler._error(401, "auth.required", "valid Session required")
            return True
        if handler.server.course_agent is None:
            handler._error(503, "ai.unavailable", "course agent is not configured")
            return True
        playground_id, version_id, messages, tools = agent_request(handler._read_json())
        turn = handler.server.course_agent.step(playground_id, version_id, messages, tools)
        handler._send_json(200, turn, (("Cache-Control", "no-store"),))
    except ValueError as error:
        handler._error(400, "ai.request.invalid", str(error))
    except CourseAiError as error:
        handler._error(502, "ai.reply.invalid", str(error))
    except _ResponseSent:
        pass
    except Exception:
        logger.exception("course agent request failed")
        handler._error(500, "server.failure", "server failure")
    return True


def _request_value(value: object) -> tuple[str, str, dict[str, object]]:
    if not isinstance(value, dict) or set(value) != {"playgroundId", "versionId", "request"}:
        raise ValueError("body must contain playgroundId, versionId and request")
    playground_id = value["playgroundId"]
    version_id = value["versionId"]
    if not isinstance(playground_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,100}", playground_id) is None:
        raise ValueError("playgroundId is invalid")
    if not isinstance(version_id, str) or not 1 <= len(version_id) <= 200:
        raise ValueError("versionId is invalid")
    request = value["request"]
    if not isinstance(request, dict) or set(request) != {"kind", "text", "quote", "context"}:
        raise ValueError("request fields are invalid")
    if request["kind"] not in ("question", "explain"):
        raise ValueError("request kind is invalid")
    if not isinstance(request["text"], str) or len(request["text"]) > 4000:
        raise ValueError("request text is invalid")
    _quote(request["quote"])
    _context(request["context"])
    return playground_id, version_id, request


def _quote(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"text", "lines", "moment"}:
        raise ValueError("request quote is invalid")
    if not isinstance(value["text"], str) or len(value["text"]) > 10_000:
        raise ValueError("request quote text is invalid")
    lines = value["lines"]
    if lines is not None and (
        not isinstance(lines, list) or len(lines) != 2
        or any(isinstance(line, bool) or not isinstance(line, int) or line < 1 for line in lines)
        or lines[1] < lines[0]
    ):
        raise ValueError("request quote lines are invalid")
    _moment(value["moment"])


def _context(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"source", "moment"}:
        raise ValueError("request context is invalid")
    if not isinstance(value["source"], str) or len(value["source"]) > 50_000:
        raise ValueError("request source is invalid")
    _moment(value["moment"])


def _moment(value: object) -> None:
    if value is None:
        return
    required = {"step", "steps", "line", "occurrence", "frames", "globals", "stdout"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("request moment is invalid")
    step, steps, line = value["step"], value["steps"], value["line"]
    if (isinstance(step, bool) or not isinstance(step, int) or step < 0
            or isinstance(steps, bool) or not isinstance(steps, int) or steps < step
            or line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1)):
        raise ValueError("request moment position is invalid")
    if not isinstance(value["frames"], list) or not isinstance(value["globals"], dict) or not isinstance(value["stdout"], str):
        raise ValueError("request moment state is invalid")
