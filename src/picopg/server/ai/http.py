from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from picopg.server.ai.budget import AiLimited, current_principal
from picopg.server.ai.step import step_request
from picopg.server.ai.model import CourseAiError
from picopg.server.learner import due, for_course
from picopg.server.replication import ResponseSent, logger

if TYPE_CHECKING:
    from picopg.server.app import PicoRequestHandler


def dispatch_ai(handler: PicoRequestHandler, path: str) -> bool:
    """AI 只有一个路由：POST /api/v1/ai/step，按 prompt 的名字分派（见 step.py）。"""
    if path != "/api/v1/ai/step":
        return False
    _guarded(handler, "ai step", lambda: _step(handler))
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


def _step(handler: PicoRequestHandler) -> None:
    principal = _permitted(handler)
    if principal is None:
        return
    name, prompt, course, parsed = step_request(handler.read_json())
    service = getattr(handler.server, prompt.service)
    if service is None:
        handler.error(503, "ai.unavailable", f"prompt {name} is not configured")
        return
    learner = _learner(handler, principal, None if course is None else course.playground_id) if prompt.tools else None
    run = lambda on_text: prompt.run(service, course, parsed, on_text, learner)  # noqa: E731
    if "application/x-ndjson" in (handler.headers.get("Accept") or ""):
        _stream(handler, run)
    else:
        handler.send_json(200, {"turn": run(None)}, (("Cache-Control", "no-store"),))


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
        logger.exception("ai step stream failed")
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
