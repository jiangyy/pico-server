from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from picopg.server.ai.agent import agent_request, tutor_request
from picopg.server.ai.diagnose import diagnose_request
from picopg.server.ai.follow import follow_request
from picopg.server.ai.hint import hint_request
from picopg.server.ai.model import CourseAiError
from picopg.server.learner import due, for_course
from picopg.server.replication import ResponseSent, logger

if TYPE_CHECKING:
    from picopg.server.app import PicoRequestHandler


def dispatch_ai(handler: PicoRequestHandler, path: str) -> bool:
    if path in ("/api/v1/ai/agent", "/api/v1/ai/tutor"):
        return _dispatch_agent(handler, path)
    if path == "/api/v1/ai/hint":
        return _dispatch_hint(handler)
    if path == "/api/v1/ai/diagnose":
        return _dispatch_diagnose(handler)
    if path == "/api/v1/ai/follow":
        return _dispatch_follow(handler)
    if path != "/api/v1/ai/course":
        return False
    try:
        if handler.command != "POST":
            handler.error(405, "method.not_allowed", "method not allowed")
            return True
        if _permitted(handler) is None:
            return True
        if handler.server.course_ai is None:
            handler.error(503, "ai.unavailable", "course AI is not configured")
            return True
        playground_id, version_id, request = _request_value(handler.read_json())
        reply = handler.server.course_ai.reply(playground_id, version_id, request)
        handler.send_json(200, reply, (("Cache-Control", "no-store"),))
    except ValueError as error:
        handler.error(400, "ai.request.invalid", str(error))
    except CourseAiError as error:
        handler.error(502, "ai.reply.invalid", str(error))
    except ResponseSent:
        pass
    except Exception:
        logger.exception("course AI request failed")
        handler.error(500, "server.failure", "server failure")
    return True


def _dispatch_agent(handler: PicoRequestHandler, path: str) -> bool:
    try:
        if handler.command != "POST":
            handler.error(405, "method.not_allowed", "method not allowed")
            return True
        principal = _permitted(handler)
        if principal is None:
            return True
        if handler.server.course_agent is None:
            handler.error(503, "ai.unavailable", "course agent is not configured")
            return True
        agent = handler.server.course_agent
        if path == "/api/v1/ai/tutor":
            messages, tools = tutor_request(handler.read_json())
            learner = _learner(handler, principal, None)
            run = lambda on_text: agent.tutor(messages, tools, on_text, learner=learner)  # noqa: E731
        else:
            playground_id, version_id, messages, tools = agent_request(handler.read_json())
            learner = _learner(handler, principal, playground_id)
            run = lambda on_text: agent.step(playground_id, version_id, messages, tools, on_text, learner=learner)  # noqa: E731
        if "application/x-ndjson" in (handler.headers.get("Accept") or ""):
            _stream(handler, run)
        else:
            handler.send_json(200, run(None), (("Cache-Control", "no-store"),))
    except ValueError as error:
        handler.error(400, "ai.request.invalid", str(error))
    except CourseAiError as error:
        handler.error(502, "ai.reply.invalid", str(error))
    except ResponseSent:
        pass
    except Exception:
        logger.exception("course agent request failed")
        handler.error(500, "server.failure", "server failure")
    return True


def _dispatch_hint(handler: PicoRequestHandler) -> bool:
    """助教在该开口的时刻请模型写提示：客户端给证据，这里补课程的规则与函数含义。"""
    try:
        if handler.command != "POST":
            handler.error(405, "method.not_allowed", "method not allowed")
            return True
        if _permitted(handler) is None:
            return True
        if handler.server.course_hint is None:
            handler.error(503, "ai.unavailable", "course hint is not configured")
            return True
        playground_id, version_id, evidence, focus = hint_request(handler.read_json())
        hint = handler.server.course_hint.hint(playground_id, version_id, evidence, focus)
        handler.send_json(200, hint, (("Cache-Control", "no-store"),))
    except ValueError as error:
        handler.error(400, "ai.request.invalid", str(error))
    except CourseAiError as error:
        handler.error(502, "ai.reply.invalid", str(error))
    except ResponseSent:
        pass
    except Exception:
        logger.exception("course hint request failed")
        handler.error(500, "server.failure", "server failure")
    return True


def _dispatch_diagnose(handler: PicoRequestHandler) -> bool:
    """助教在后台诊断：客户端给证据、参考解和没验证过的几次补丁，这里补课程的规则与概念清单。"""
    try:
        if handler.command != "POST":
            handler.error(405, "method.not_allowed", "method not allowed")
            return True
        if _permitted(handler) is None:
            return True
        if handler.server.course_diagnose is None:
            handler.error(503, "ai.unavailable", "course diagnose is not configured")
            return True
        playground_id, version_id, request = diagnose_request(handler.read_json())
        handler.send_json(200, handler.server.course_diagnose.diagnose(playground_id, version_id, request), (("Cache-Control", "no-store"),))
    except ValueError as error:
        handler.error(400, "ai.request.invalid", str(error))
    except CourseAiError as error:
        handler.error(502, "ai.reply.invalid", str(error))
    except ResponseSent:
        pass
    except Exception:
        logger.exception("course diagnose request failed")
        handler.error(500, "server.failure", "server failure")
    return True


def _dispatch_follow(handler: PicoRequestHandler) -> bool:
    """助教的“跟”：客户端带上整条线程，这里补课程前缀，拿回下一份思路笔记。"""
    try:
        if handler.command != "POST":
            handler.error(405, "method.not_allowed", "method not allowed")
            return True
        if _permitted(handler) is None:
            return True
        if handler.server.course_follow is None:
            handler.error(503, "ai.unavailable", "course follow is not configured")
            return True
        playground_id, version_id, request = follow_request(handler.read_json())
        handler.send_json(200, handler.server.course_follow.follow(playground_id, version_id, request), (("Cache-Control", "no-store"),))
    except ValueError as error:
        handler.error(400, "ai.request.invalid", str(error))
    except CourseAiError as error:
        handler.error(502, "ai.reply.invalid", str(error))
    except ResponseSent:
        pass
    except Exception:
        logger.exception("course follow request failed")
        handler.error(500, "server.failure", "server failure")
    return True


def _stream(handler: PicoRequestHandler, run: Callable[[Callable[[str], None] | None], dict[str, object]]) -> None:
    """一行一个 JSON：正文片段 {"text"} 随模型到达，最后一行是这一步 {"turn"}，失败时是 {"error"}。
    响应头发出后不能再改状态码，所以中途失败也写成一行。"""
    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    # nginx 默认攒满缓冲区才转发；这个头让它逐行放行。
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    handler.close_connection = True

    def line(value: dict[str, object]) -> None:
        handler.wfile.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        handler.wfile.flush()

    try:
        turn = run(lambda text: line({"text": text}))
    except (BrokenPipeError, ConnectionResetError):
        raise ResponseSent() from None
    except CourseAiError as error:
        line({"error": {"code": "ai.reply.invalid", "message": str(error)}})
        raise ResponseSent() from None
    except Exception:
        logger.exception("course agent stream failed")
        line({"error": {"code": "server.failure", "message": "server failure"}})
        raise ResponseSent() from None
    line({"turn": turn})
    raise ResponseSent()


def _permitted(handler: PicoRequestHandler) -> str | None:
    """允许时返回提问的学生。"""
    replica = handler.authenticated_replica()
    if replica is None:
        handler.error(401, "auth.required", "valid Session required")
        return None
    if not handler.server.ai_access.allows(replica.principal_id):
        handler.error(403, "ai.not_permitted", "AI is not enabled for this principal")
        return None
    if not handler.server.ai_budget.admit(replica.principal_id):
        handler.error(429, "ai.limited", "AI request limit reached; try again later")
        return None
    return replica.principal_id


def _learner(handler: PicoRequestHandler, principal: str, course: str | None) -> dict[str, object] | None:
    """学生画像：课程里只给本课相关的部分，导师给全部。算不出来时照常回答，只是不带画像。"""
    try:
        profile = handler.server.learners.profile(principal)
        now = datetime.now(timezone.utc)
        if course is None:
            return {**profile, "due": due(profile, now)}
        return for_course(profile, course, handler.server.lessons.knowledge(), now)
    except Exception:
        logger.exception("learner profile failed")
        return None


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
