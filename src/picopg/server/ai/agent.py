from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from picopg.server.ai.course import CourseSkillRepository
from picopg.server.ai.model import CourseAiError, ToolModel


class CourseAgent(Protocol):
    def step(
        self, playground_id: str, version_id: str,
        messages: list[dict[str, object]], tools: list[dict[str, object]],
        on_text: Callable[[str], None] | None = None, learner: dict[str, object] | None = None,
    ) -> dict[str, object]: ...

    def tutor(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]],
        on_text: Callable[[str], None] | None = None, learner: dict[str, object] | None = None,
    ) -> dict[str, object]: ...

    def draft_comment(self, playground_id: str, version_id: str, source: str, learner: dict[str, object] | None = None) -> str: ...


MAX_MESSAGES = 240
MAX_CONTENT = 60_000
MAX_TOOLS = 16
MAX_TOOL_BYTES = 32_000
TEACHING = Path(__file__).with_name("teaching")


@dataclass(frozen=True)
class TeachingSkill:
    """teaching/<序号>-<名字>.md：头部 when 决定何时加载——always、tool <工具名>、ask <学生原话的正则>。"""

    name: str
    when: str
    body: str

    def applies(self, tools: set[str], said: str) -> bool:
        kind, _, value = self.when.partition(" ")
        if kind == "always":
            return True
        if kind == "tool":
            return value in tools
        return kind == "ask" and re.search(value, said) is not None


def load_teaching(root: Path = TEACHING) -> list[TeachingSkill]:
    """按文件名的序号排列：不随本轮变化的在前；未匹配的 skill 不占 token。"""
    skills = []
    for path in sorted(root.glob("*.md")):
        head, body = path.read_text(encoding="utf-8").removeprefix("---\n").split("\n---\n", 1)
        when = dict(line.split(": ", 1) for line in head.splitlines())["when"]
        skills.append(TeachingSkill(path.stem.split("-", 1)[-1], when, body.strip()))
    return skills


class CourseAgentService:
    """无状态的一步：对话与工具由客户端带来（工具在学生的会话里执行），这里只加系统提示与课程 skill 并调用模型。"""

    TUTOR = """你是 Pico，学生在整个学习平台上的导师，面对的是初中生。你看得到这个学生的全部课程、打开过的课、提交与老师的批注，以及知识地图。
你负责：接下来学什么、学得怎么样、老师的批注说了什么、某门课练什么。具体某一行代码的问题，请学生打开那门课问课程里的 AI。

怎么做：
- 只根据用户消息里的学习数据说话，不要编造课程、提交或批注。
- 先用话把问题答完整，按钮只是补充。大多数回答不需要按钮：问学得怎么样、评语说了什么、某个概念是什么时，直接用话回答，不要调用 offer。
- 只有学生问接下来学什么、或明确想去某门课时，才用 offer 放上「打开」或「回到」这门课的按钮，按钮最多 3 个。
- 问学得怎么样时，根据提交、完成与批注说出具体进展：做得好的地方、还要加强的一个点、下一步可以怎么练。
- 学生想学某个知识点时，先用两三句话和一个小例子讲清它是什么、为什么有用，再用 search_lessons 找讲它的那一步（工具存在时），用 offer 的 lecture 按钮带过去，lecture 与 step 照搬搜索结果；搜不到就如实说现有课程没讲到。
- 解释老师的批注时引用学习数据里的批注原文与它批在第几行，不要只说条数。
- 系统提示最后有学生画像时，问学得怎么样、接下来学什么，都以它为据：先说 shaky 的概念和反复出现的误区，再说 due 里该复习的、open 里没做完的练习；用概念的中文名，不说 id 和状态值。
- 说清楚为止，一般 3 到 8 句：具体、鼓励，可以用 Markdown。"""

    DRAFT = """你在帮初中 Python 老师起草一条对学生作业提交的评语，老师会改过再发给学生。
你看不到运行结果，只能读代码；拿不准的地方用「可以运行看看……」提醒，不要断言。
评语是纯文本，学生看到的就是你写的字：不用 Markdown 记号（不要 **、`、#），每项单独一行，总共不超过 120 字，只输出评语正文：
做对了：具体到哪一行、哪个想法。
问题：哪一行、为什么；没有明显问题就写一个可以改进的地方。
怎么改：给方向或提示，不给完整答案。
最后一行是一句简短的鼓励。
附有学生画像时，留意本次代码是否又出现画像里的误区或不牢的概念，有就点出来。"""

    def __init__(self, model: ToolModel, skills: CourseSkillRepository, teaching: list[TeachingSkill] | None = None) -> None:
        self.model = model
        self.skills = skills
        self._teaching = teaching
        self.teaching = load_teaching() if teaching is None else teaching

    def step(
        self, playground_id: str, version_id: str,
        messages: list[dict[str, object]], tools: list[dict[str, object]],
        on_text: Callable[[str], None] | None = None, learner: dict[str, object] | None = None,
    ) -> dict[str, object]:
        system = self.system(playground_id, version_id, messages, tools) + _learner(learner)
        chat = [{"role": "system", "content": system}, *(_chat_message(message) for message in messages)]
        reply = self.model.step(chat, [{"type": "function", "function": tool} for tool in tools], _hiding_calls(on_text))
        return _turn(reply, {str(tool["name"]) for tool in tools})

    def system(
        self, playground_id: str, version_id: str,
        messages: list[dict[str, object]], tools: list[dict[str, object]],
    ) -> str:
        """稳定的部分在前（core、本课要点），随本轮变化的 skill 在后，模型服务的前缀缓存才命中。"""
        names = {str(tool["name"]) for tool in tools}
        said = _said(messages)
        teaching = load_teaching() if self._teaching is None else self.teaching
        matched = [skill for skill in teaching if skill.applies(names, said)]
        parts = [skill.body for skill in matched if skill.when == "always"]
        course = self._skill(playground_id, version_id)
        if course is not None:
            parts.append("本课的讲解要点（JSON）：\n" + json.dumps(course, ensure_ascii=False, separators=(",", ":")))
        parts += [skill.body for skill in matched if skill.when != "always"]
        return "\n\n".join(parts)

    def tutor(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]],
        on_text: Callable[[str], None] | None = None, learner: dict[str, object] | None = None,
    ) -> dict[str, object]:
        chat = [{"role": "system", "content": self.TUTOR + _learner(learner)}, *(_chat_message(message) for message in messages)]
        return _turn(self.model.step(chat, [{"type": "function", "function": tool} for tool in tools], _hiding_calls(on_text)), {str(tool["name"]) for tool in tools})

    def draft_comment(self, playground_id: str, version_id: str, source: str, learner: dict[str, object] | None = None) -> str:
        """老师批改时的草稿：不带工具，一步写完。"""
        system = self.DRAFT
        course = self._skill(playground_id, version_id)
        if course is not None:
            system += "\n\n本课的讲解要点（JSON）：\n" + json.dumps(course, ensure_ascii=False, separators=(",", ":"))
        numbered = "\n".join(f"{index}| {line}" for index, line in enumerate(source.splitlines()[:400], 1))
        chat = [{"role": "system", "content": system + _learner(learner)}, {"role": "user", "content": f"学生提交的代码：\n{numbered}"}]
        text = _turn(self.model.step(chat, [], None))["content"]
        if not text.strip():
            raise CourseAiError("model returned an empty draft")
        return text.strip()

    def _skill(self, playground_id: str, version_id: str) -> dict[str, object] | None:
        try:
            skill = self.skills.read(playground_id, version_id)
        except CourseAiError:
            return None
        return {key: value for key, value in skill.items() if key not in ("actions", "course")}


def _learner(learner: dict[str, object] | None) -> str:
    """画像随学生的新证据变化，放在系统提示的最后，前面的部分仍能命中前缀缓存。"""
    if not learner:
        return ""
    return (
        "\n\n学生画像（JSON；用来决定教什么、问什么，不要念给学生听）。"
        "status：shaky 不牢、learning 在学、solid 掌握；misconceptions：反复出现的误区；open：没做完的练习；due：该复习的概念；"
        "habits.askedBeforeSubmit：交练习前平均问 AI 几次（低说明先自己试，是好习惯）；habits.attemptsToPass：平均第几次交才通过。\n"
        + json.dumps(learner, ensure_ascii=False, separators=(",", ":"))
    )


def _said(messages: list[dict[str, object]]) -> str:
    """学生最近一次说的话：用户消息的第一段（其后是客户端附上的代码与快照）。"""
    for message in reversed(messages):
        if message["role"] == "user":
            return str(message["content"]).split("\n\n", 1)[0]
    return ""


def tutor_request(value: object) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if not isinstance(value, dict) or set(value) != {"messages", "tools"}:
        raise ValueError("body must contain messages and tools")
    _, _, messages, tools = agent_request({"playgroundId": "tutor", "versionId": "tutor", **value})
    return messages, tools


def agent_request(value: object) -> tuple[str, str, list[dict[str, object]], list[dict[str, object]]]:
    if not isinstance(value, dict) or set(value) != {"playgroundId", "versionId", "messages", "tools"}:
        raise ValueError("body must contain playgroundId, versionId, messages and tools")
    playground_id, version_id = value["playgroundId"], value["versionId"]
    if not isinstance(playground_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,100}", playground_id) is None:
        raise ValueError("playgroundId is invalid")
    if not isinstance(version_id, str) or not 1 <= len(version_id) <= 200:
        raise ValueError("versionId is invalid")
    messages, tools = value["messages"], value["tools"]
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise ValueError("messages are invalid")
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
        raise ValueError("tools are invalid")
    return playground_id, version_id, [_message(item) for item in messages], [_tool(item) for item in tools]


def _message(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("message must be an object")
    role = value.get("role")
    content = value.get("content")
    if not isinstance(content, str) or len(content) > MAX_CONTENT:
        raise ValueError("message content is invalid")
    if role == "user" and set(value) == {"role", "content"}:
        return value
    if role == "tool" and set(value) == {"role", "call", "content"} and _name(value["call"]):
        return value
    if role == "assistant" and set(value) - {"role", "content", "calls", "reasoning"} == set():
        calls = value.get("calls", [])
        reasoning = value.get("reasoning", "")
        if not isinstance(calls, list) or len(calls) > 16 or not isinstance(reasoning, str) or len(reasoning) > MAX_CONTENT:
            raise ValueError("assistant message is invalid")
        for call in calls:
            if (not isinstance(call, dict) or set(call) != {"id", "name", "arguments"}
                    or not _name(call["id"]) or not _name(call["name"])
                    or not isinstance(call["arguments"], str) or len(call["arguments"]) > MAX_CONTENT):
                raise ValueError("assistant tool call is invalid")
        return value
    raise ValueError("message role or fields are invalid")


def _tool(value: object) -> dict[str, object]:
    if (not isinstance(value, dict) or set(value) != {"name", "description", "parameters"}
            or not _name(value["name"]) or not isinstance(value["description"], str)
            or not isinstance(value["parameters"], dict)):
        raise ValueError("tool is invalid")
    if len(json.dumps(value, ensure_ascii=False)) > MAX_TOOL_BYTES:
        raise ValueError("tool is too large")
    return value


def _name(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value) is not None


def _chat_message(message: dict[str, object]) -> dict[str, object]:
    if message["role"] == "tool":
        return {"role": "tool", "tool_call_id": message["call"], "content": message["content"]}
    if message["role"] == "user":
        return {"role": "user", "content": message["content"]}
    chat: dict[str, object] = {"role": "assistant", "content": message["content"]}
    calls = message.get("calls") or []
    if calls:
        chat["tool_calls"] = [
            {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": call["arguments"]}}
            for call in calls
        ]
    if message.get("reasoning"):
        chat["reasoning_content"] = message["reasoning"]
    return chat


# 有的模型把工具调用写成正文 functions.名字({...})，而不是 tool_calls。
_WRITTEN_CALL = re.compile(r"functions\.([A-Za-z_]\w*)\s*\(\s*(\{.*?\})\s*\)", re.S)
_WRITTEN_CALL_START = re.compile(r"functions\.\w*(\s*\(.*)?$", re.S)


def _hiding_calls(on_text: Callable[[str], None] | None) -> Callable[[str], None] | None:
    """边生成边给学生看的文字，不带写成正文的调用（包括还没写完的那一截）。"""
    if on_text is None:
        return None
    return lambda text: on_text(_WRITTEN_CALL_START.sub("", _WRITTEN_CALL.sub("", text)).rstrip())


def _recover_calls(content: str, names: set[str]) -> tuple[str, list[dict[str, object]]]:
    """名字是本轮的工具、参数是 JSON 对象的写成正文的调用，当作真的调用并从正文里删掉。"""
    calls: list[dict[str, object]] = []

    def take(match: re.Match[str]) -> str:
        name, arguments = match.group(1), match.group(2)
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return match.group(0)
        if name not in names or not isinstance(parsed, dict):
            return match.group(0)
        calls.append({"id": f"written-{uuid.uuid4().hex[:12]}", "name": name, "arguments": arguments})
        return ""

    stripped = _WRITTEN_CALL.sub(take, content)
    return (stripped.strip(), calls) if calls else (content, calls)


def _turn(reply: dict[str, object], names: set[str] | None = None) -> dict[str, object]:
    content = reply.get("content")
    calls = []
    for call in reply.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or not isinstance(call.get("id"), str):
            raise CourseAiError("model returned an invalid tool call")
        calls.append({
            "id": call["id"],
            "name": str(function.get("name", "")),
            "arguments": function.get("arguments") if isinstance(function.get("arguments"), str) else "{}",
        })
    text = content if isinstance(content, str) else ""
    if not calls and names:
        text, calls = _recover_calls(text, names)
    turn: dict[str, object] = {"content": text, "calls": calls}
    reasoning = reply.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        turn["reasoning"] = reasoning
    if not turn["content"].strip() and not calls:
        raise CourseAiError("model returned neither text nor tool calls")
    return turn
