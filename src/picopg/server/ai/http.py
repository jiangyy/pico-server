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
from picopg.server.ai.debrief import debrief_request
from picopg.server.ai.budget import AiLimited, current_principal
from picopg.server.ai.model import CourseAiError
from picopg.server.learner import due, for_course
from picopg.server.replication import ResponseSent, logger
from picopg.server.common.validation import is_int

if TYPE_CHECKING:
    from picopg.server.app import PicoRequestHandler


#: 一个运行时刻（调用栈、全局变量、输出）序列化后的上限，和源码的上限同一量级。
MOMENT_LIMIT = 50_000


def dispatch_ai(handler: PicoRequestHandler, path: str) -> bool:
    if path in ("/api/v1/ai/agent", "/api/v1/ai/tutor"):
        _guarded(handler, "course agent", lambda: _agent(handler, path))
        return True
    route = _ROUTES.get(path)
    if route is None:
        return False
    label, attribute, parse, call = route

    def answer() -> None:
        if _permitted(handler) is None:
            return
        service = getattr(handler.server, attribute)
        if service is None:
            handler.error(503, "ai.unavailable", f"{label} is not configured")
            return
        handler.send_json(200, call(service, parse(handler.read_json())), (("Cache-Control", "no-store"),))

    _guarded(handler, label, answer)
    return True


def _guarded(handler: PicoRequestHandler, label: str, body: Callable[[], None]) -> None:
    """AI 接口共同的外壳：只收 POST；请求不对是 400，模型答得不合格是 502，其余意外是 500。"""
    try:
        if handler.command != "POST":
            handler.error(405, "method.not_allowed", "method not allowed")
            return
        body()
    except ValueError as error:
        handler.error(400, "ai.request.invalid", str(error))
    except CourseAiError as error:
        handler.error(502, "ai.reply.invalid", str(error))
    except AiLimited as error:
        handler.error(429, "ai.limited", str(error))


def _agent(handler: PicoRequestHandler, path: str) -> None:
    principal = _permitted(handler)
    if principal is None:
        return
    if handler.server.course_agent is None:
        handler.error(503, "ai.unavailable", "course agent is not configured")
        return
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
    except AiLimited as error:
        line({"error": {"code": "ai.limited", "message": str(error)}})
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
    # 放行与记账在每一次模型调用时做（一次请求可能调用几次模型）：这里只记下是谁在用。
    current_principal.set(replica.principal_id)
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
        or any(not is_int(line) or line < 1 for line in lines)
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
    if (not is_int(step) or step < 0
            or not is_int(steps) or steps < step
            or line is not None and (not is_int(line) or line < 1)):
        raise ValueError("request moment position is invalid")
    if not isinstance(value["frames"], list) or not isinstance(value["globals"], dict) or not isinstance(value["stdout"], str):
        raise ValueError("request moment state is invalid")
    # 和相邻字段一样有上限：运行时的变量可以很大，原样整份送进模型既慢又贵。
    if len(json.dumps([value["frames"], value["globals"], value["stdout"]], ensure_ascii=False)) > MOMENT_LIMIT:
        raise ValueError("request moment is too large")


#: 一问一答的几项服务：(名字, Server 上的属性, 请求校验, 调用)。
#: 课程 AI 一问一答；提示写在该开口的时刻；诊断在后台找 bug；跟随写下一份思路笔记；复盘在做对之后。
_ROUTES: dict[str, tuple[str, str, Callable[[object], tuple], Callable[[object, tuple], dict[str, object]]]] = {
    "/api/v1/ai/course": ("course AI", "course_ai", _request_value, lambda service, args: service.reply(*args)),
    "/api/v1/ai/hint": ("course hint", "course_hint", hint_request, lambda service, args: service.hint(*args)),
    "/api/v1/ai/diagnose": ("course diagnose", "course_diagnose", diagnose_request, lambda service, args: service.diagnose(*args)),
    "/api/v1/ai/follow": ("course follow", "course_follow", follow_request, lambda service, args: service.follow(*args)),
    "/api/v1/ai/debrief": ("course debrief", "course_debrief", debrief_request, lambda service, args: service.debrief(*args)),
}
