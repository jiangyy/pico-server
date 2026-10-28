from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol
from urllib.request import Request, urlopen


class CourseAiError(Exception):
    pass


class CourseAi(Protocol):
    def reply(self, playground_id: str, version_id: str, request: dict[str, object]) -> dict[str, object]: ...


class ChatModel(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> str: ...


class CourseSkillRepository:
    def __init__(self, root: Path) -> None:
        self.root = root

    def read(self, playground_id: str) -> dict[str, object]:
        path = self.root / f"{playground_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise CourseAiError(f"course AI is not configured for {playground_id!r}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise CourseAiError(f"course AI skill is invalid for {playground_id!r}") from error
        if not isinstance(value, dict) or value.get("course") != playground_id:
            raise CourseAiError(f"course AI skill does not match {playground_id!r}")
        actions = value.get("actions")
        if not isinstance(actions, list) or any(not isinstance(item, str) for item in actions):
            raise CourseAiError(f"course AI skill actions are invalid for {playground_id!r}")
        return value


class OpenAiCompatibleChatModel:
    def __init__(self, base_url: str, model: str, api_key: str | None = None, effort: str | None = None) -> None:
        if not base_url.startswith(("http://", "https://")) or not model:
            raise ValueError("AI base URL must be HTTP(S) and model must be non-empty")
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.model = model
        self.api_key = api_key
        self.effort = effort

    def complete(self, messages: list[dict[str, str]]) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            with urlopen(Request(self.url, body, headers, method="POST"), timeout=45) as response:
                value = json.loads(response.read().decode("utf-8"))
            content = value["choices"][0]["message"]["content"]
        except Exception as error:
            raise CourseAiError("model request failed") from error
        if not isinstance(content, str) or not content.strip():
            raise CourseAiError("model returned no content")
        return content

    def step(self, messages: list[dict[str, object]], tools: list[dict[str, object]]) -> dict[str, object]:
        """一次带工具的补全，返回 assistant 消息原样（content、tool_calls、reasoning_content）。"""
        request: dict[str, object] = {"model": self.model, "messages": messages, "temperature": 0.3}
        if tools:
            request["tools"] = tools
        if self.effort:
            request["reasoning_effort"] = self.effort
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
        try:
            with urlopen(Request(self.url, body, headers, method="POST"), timeout=120) as response:
                message = json.loads(response.read().decode("utf-8"))["choices"][0]["message"]
        except Exception as error:
            raise CourseAiError("model request failed") from error
        if not isinstance(message, dict):
            raise CourseAiError("model returned no message")
        return message


class CourseAiService:
    SYSTEM = (
        "你是面向初中生的 Python 学习助手。只使用给定事实，一次只解释一件事，"
        "结尾给一个小问题；不要直接贴完整答案。只输出 JSON："
        '{"title":"标题","text":"正文","primary":{"action":"动作 id","label":"按钮文字",'
        '"args":{}},"actions":[]}。primary、actions 和动作 label 可以省略；动作不能只写 id 字符串。'
        'trace.seek 的 args 是 {"step":当前步整数}；code.locate 是 {"line":源码行号}；'
        'annotation.show 用 {"kind":"mark|note","at":{"line":源码行号},"text":"短批注"}；'
        'source.explain 用 {"source":"完整临时代码"}，必须以 context.source 为底稿且只做讲解所需的最小改动；'
        "selection.locate、code.run 和 source.restore 没有参数。临时代码只用于演示，结束对话会恢复学生原稿。"
        "能用批注说明时不要改代码；不确定参数时省略动作。"
    )

    def __init__(self, model: ChatModel, skills: CourseSkillRepository) -> None:
        self.model = model
        self.skills = skills

    def reply(self, playground_id: str, version_id: str, request: dict[str, object]) -> dict[str, object]:
        skill = self.skills.read(playground_id)
        focused = _focused_skill(skill, request)
        messages = [
            {"role": "system", "content": self.SYSTEM},
            {"role": "user", "content": json.dumps({
                "course": playground_id,
                "version": version_id,
                "skill": focused,
                "request": request,
            }, ensure_ascii=False, separators=(",", ":"))},
        ]
        error: CourseAiError | None = None
        for _attempt in range(2):
            content = self.model.complete(messages)
            try:
                return _reply_value(content, skill, request)
            except CourseAiError as caught:
                error = caught
                messages.extend([
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": (
                        f"上个 JSON 无效：{caught}。只重写合法 JSON；"
                        '动作格式必须是 {"action":"允许的 id","args":{}}，不能只写 id 字符串。'
                    )},
                ])
        raise CourseAiError("model returned invalid replies twice") from error


def _focused_skill(skill: dict[str, object], request: dict[str, object]) -> dict[str, object]:
    context = request.get("context")
    source = context.get("source", "") if isinstance(context, dict) else ""
    corpus = f"{request.get('text', '')}\n{source}".lower()
    misconceptions = skill.get("misconceptions", [])
    matched = []
    if isinstance(misconceptions, list):
        for item in misconceptions:
            if not isinstance(item, dict):
                continue
            signs = item.get("signs", [])
            if isinstance(signs, list) and any(
                isinstance(sign, str) and sign.lower() in corpus for sign in signs
            ):
                matched.append(item)
            if len(matched) == 2:
                break
    return {
        key: value for key, value in skill.items()
        if key not in ("misconceptions", "actions")
    } | {"misconceptions": matched, "actions": skill["actions"]}


def _reply_value(content: str, skill: dict[str, object], request: dict[str, object]) -> dict[str, object]:
    candidate = content.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise CourseAiError("reply is not JSON") from error
    if not isinstance(value, dict) or set(value) - {"title", "text", "primary", "actions"}:
        raise CourseAiError("reply fields are invalid")
    text = value.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise CourseAiError("reply text is invalid")
    if "```" in text:
        raise CourseAiError("reply contains a full code block")
    title = value.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > 120):
        raise CourseAiError("reply title is invalid")
    context = request.get("context")
    source = context.get("source", "") if isinstance(context, dict) else ""
    lines = max(1, str(source).count("\n") + 1)
    for mentioned in re.findall(r"(?:第\s*)?(\d+)\s*行", text):
        if not 1 <= int(mentioned) <= lines:
            raise CourseAiError("reply cites a line outside the source")
    allowed = frozenset(skill["actions"])
    primary = value.get("primary")
    if primary is not None:
        value["primary"] = _action_value(primary, allowed, request)
    actions = value.get("actions")
    if actions is not None:
        if not isinstance(actions, list) or len(actions) > 3:
            raise CourseAiError("reply actions are invalid")
        value["actions"] = [_action_value(item, allowed, request) for item in actions]
    return value


def _action_value(value: object, allowed: frozenset[str], request: dict[str, object]) -> dict[str, object]:
    if isinstance(value, str):
        value = {"action": value}
    if not isinstance(value, dict) or set(value) - {"action", "id", "label", "args"}:
        raise CourseAiError("each action must name an allowed action and optional args")
    action, alias, args = value.get("action"), value.get("id"), value.get("args")
    if action is None:
        action = alias
    elif alias is not None and alias != action:
        raise CourseAiError("action and id must not disagree")
    if not isinstance(action, str) or action not in allowed or action not in _ACTION_LABELS:
        raise CourseAiError("action id must be allowed")
    if args is not None and not isinstance(args, dict):
        raise CourseAiError("action args are invalid")
    if action in ("selection.locate", "code.run", "source.restore") and args not in (None, {}):
        raise CourseAiError("action does not accept args")
    context = request.get("context")
    source = context.get("source", "") if isinstance(context, dict) else ""
    moment = context.get("moment") if isinstance(context, dict) else None
    if action == "code.locate":
        if args is None and isinstance(moment, dict) and isinstance(moment.get("line"), int):
            args = {"line": moment["line"]}
        if not isinstance(args, dict) or set(args) - {"line", "occurrence"}:
            raise CourseAiError("code.locate requires args with a source line")
        line = args.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= str(source).count("\n") + 1:
            raise CourseAiError("code.locate line is invalid")
        occurrence = args.get("occurrence")
        if occurrence is not None and (isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 1):
            raise CourseAiError("code.locate occurrence is invalid")
    if action == "trace.seek":
        steps = moment.get("steps") if isinstance(moment, dict) else None
        if args is None and isinstance(moment, dict) and isinstance(moment.get("step"), int):
            args = {"step": moment["step"]}
        step = args.get("step") if isinstance(args, dict) else None
        if isinstance(step, bool) or not isinstance(step, int) or not isinstance(steps, int) or not 1 <= step <= steps:
            raise CourseAiError("trace.seek requires args with an integer step inside the current Trace")
    if action == "annotation.show":
        if not isinstance(args, dict) or set(args) - {"kind", "at", "to", "text"}:
            raise CourseAiError("annotation.show args are invalid")
        kind = args.get("kind")
        if kind not in ("mark", "note", "link"):
            raise CourseAiError("annotation.show kind is invalid")
        _source_anchor(args.get("at"), source)
        if kind == "link":
            _source_anchor(args.get("to"), source)
        elif "to" in args:
            raise CourseAiError("only link annotations accept to")
        note = args.get("text")
        if kind == "note" and not isinstance(note, str):
            raise CourseAiError("note annotations require text")
        if note is not None and (not isinstance(note, str) or not note.strip() or len(note) > 120):
            raise CourseAiError("annotation text is invalid")
    if action == "source.explain":
        if not isinstance(args, dict) or set(args) != {"source"}:
            raise CourseAiError("source.explain requires complete temporary source")
        temporary = args["source"]
        if not isinstance(temporary, str) or not temporary.strip() or len(temporary) > 50_000:
            raise CourseAiError("source.explain source is invalid")
    return {
        "action": action,
        "label": _ACTION_LABELS[action],
        **({} if args is None else {"args": args}),
    }


_ACTION_LABELS = {
    "selection.locate": "定位执行",
    "code.locate": "定位这一行",
    "trace.seek": "跳到这一步",
    "code.run": "运行代码",
    "annotation.show": "在代码上标出来",
    "source.explain": "在临时代码中演示",
    "source.restore": "恢复我的代码",
}


def _source_anchor(value: object, source: object) -> None:
    if not isinstance(value, dict) or set(value) - {"line", "text"} or "line" not in value:
        raise CourseAiError("annotation source anchor is invalid")
    line = value["line"]
    lines = str(source).splitlines() or [""]
    if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= len(lines):
        raise CourseAiError("annotation source line is invalid")
    text = value.get("text")
    if text is not None and (not isinstance(text, str) or not text or text not in lines[line - 1]):
        raise CourseAiError("annotation source text is invalid")
