from __future__ import annotations

import json
import re
from typing import Protocol

from picopg.server.ai.course import CourseSkillRepository
from picopg.server.ai.model import CourseAiError, ToolModel


class CourseAgent(Protocol):
    def step(
        self, playground_id: str, version_id: str,
        messages: list[dict[str, object]], tools: list[dict[str, object]],
    ) -> dict[str, object]: ...

    def tutor(self, messages: list[dict[str, object]], tools: list[dict[str, object]]) -> dict[str, object]: ...


MAX_MESSAGES = 240
MAX_CONTENT = 60_000
MAX_TOOLS = 16
MAX_TOOL_BYTES = 32_000


class CourseAgentService:
    """无状态的一步：对话与工具由客户端带来（工具在学生的会话里执行），这里只加系统提示与课程 skill 并调用模型。"""

    SYSTEM = """你是 Pico 的 Python 助教，就在学生的 IDE 里，面对的是初中生。你可以用工具操作这个 IDE：
read 读取代码、运行结果和变量；act 调用编辑器、调试器、终端、可视化等组件的动作；teach 播放一段你写的讲解讲稿；ask_to_write 请学生动手写代码。

怎么做：
- 先看事实再说话。代码、输出、变量值都来自用户消息里的上下文或 read 的结果，不要编造。
- 能演示就不要只用嘴说：用 code.locate 或 trace.seek 带学生看到具体的行和步骤；用 annotation.show 在代码上画框（mark）或写短批注（note，40 字以内）；
  需要改代码演示时用 source.explain 写一份完整的临时代码，再 act code.run 运行并读结果。对话结束时学生原来的代码会自动恢复。
- 问题需要连贯的分步讲解（像老师一边讲一边写代码）时，用 teach 写一份讲稿；讲稿开始播放后这一轮就结束了。
- 想让学生自己练：先用一两句话布置一个具体的小任务，再调用 ask_to_write。学生写好后会把代码发回来：
  先 act code.run 运行，再 read 输出，用标注指出具体的行，然后评价——做对了什么、哪里有问题、下一步怎么改。不要直接给出完整答案。
- 对学生说的话要短：一次只讲一件事，最多 5 句，可以用 Markdown 和 `代码`。
- 工具失败时读返回的原因，换参数或换办法，不要重复同样的调用。"""

    TUTOR = """你是 Pico，学生在整个学习平台上的导师，面对的是初中生。你看得到这个学生的全部课程、打开过的课、提交与老师的批注，以及知识地图。
你负责：接下来学什么、学得怎么样、老师的批注说了什么、某门课练什么。具体某一行代码的问题，请学生打开那门课问课程里的 AI。

怎么做：
- 只根据用户消息里的学习数据说话，不要编造课程、提交或批注。
- 推荐课程时用 offer 放上「打开」或「回到」这门课的按钮，按钮最多 3 个。
- 对学生说的话要短：最多 5 句，具体、鼓励，可以用 Markdown。"""

    def __init__(self, model: ToolModel, skills: CourseSkillRepository) -> None:
        self.model = model
        self.skills = skills

    def step(
        self, playground_id: str, version_id: str,
        messages: list[dict[str, object]], tools: list[dict[str, object]],
    ) -> dict[str, object]:
        system = self.SYSTEM
        skill = self._skill(playground_id)
        if skill is not None:
            system += "\n\n本课的讲解要点（JSON）：\n" + json.dumps(skill, ensure_ascii=False, separators=(",", ":"))
        chat = [{"role": "system", "content": system}, *(_chat_message(message) for message in messages)]
        reply = self.model.step(chat, [{"type": "function", "function": tool} for tool in tools])
        return _turn(reply)

    def tutor(self, messages: list[dict[str, object]], tools: list[dict[str, object]]) -> dict[str, object]:
        chat = [{"role": "system", "content": self.TUTOR}, *(_chat_message(message) for message in messages)]
        return _turn(self.model.step(chat, [{"type": "function", "function": tool} for tool in tools]))

    def _skill(self, playground_id: str) -> dict[str, object] | None:
        try:
            skill = self.skills.read(playground_id)
        except CourseAiError:
            return None
        return {key: value for key, value in skill.items() if key not in ("actions", "course")}


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


def _turn(reply: dict[str, object]) -> dict[str, object]:
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
    turn: dict[str, object] = {"content": content if isinstance(content, str) else "", "calls": calls}
    reasoning = reply.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        turn["reasoning"] = reasoning
    if not turn["content"].strip() and not calls:
        raise CourseAiError("model returned neither text nor tool calls")
    return turn
