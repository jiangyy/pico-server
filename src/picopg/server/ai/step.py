"""一个路由跑所有 prompt：POST /api/v1/ai/step {prompt, course, input} → {turn}。

prompt 是服务端的名字，客户端只选用哪一个；系统提示、课程知识与学习者画像都在服务端拼，客户端带不进来。
带工具的 prompt（course.agent、pico.tutor）是多步推理的一步，input 是 {messages, tools}，回答可以流式；
其余的一步写完，turn.value 是校验过的结果。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from picopg.server.ai.agent import agent_request, tutor_request
from picopg.server.ai.debrief import debrief_request
from picopg.server.ai.diagnose import diagnose_request
from picopg.server.ai.follow import follow_request
from picopg.server.ai.hint import hint_request

Turn = dict[str, object]
Learner = dict[str, object] | None


@dataclass(frozen=True)
class Course:
    playground_id: str
    version_id: str


@dataclass(frozen=True)
class Prompt:
    #: Server 上提供它的服务。
    service: str
    #: 要不要课程；要时 course 必须给出。
    course: bool
    #: 多步推理：带学习者画像，回答可以流式。
    tools: bool
    #: 校验 input，交给 run；坏的 input 抛 ValueError。
    parse: Callable[[Course | None, object], object]
    run: Callable[[object, Course | None, object, Callable[[str], None] | None, Learner], Turn]


def _answered(value: dict[str, object]) -> Turn:
    return {"content": "", "calls": [], "value": value}


def _course(course: Course | None) -> dict[str, str]:
    return {} if course is None else {"playgroundId": course.playground_id, "versionId": course.version_id}


def _fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("input must be an object")
    return value


PROMPTS: dict[str, Prompt] = {
    "course.agent": Prompt(
        "course_agent", course=True, tools=True,
        parse=lambda course, value: agent_request({**_course(course), **_fields(value)})[2:],
        run=lambda agent, course, parsed, on_text, learner: agent.step(  # type: ignore[attr-defined]
            course.playground_id, course.version_id, *parsed, on_text, learner=learner),  # type: ignore[union-attr, misc]
    ),
    "pico.tutor": Prompt(
        "course_agent", course=False, tools=True,
        parse=lambda _course, value: tutor_request(value),
        run=lambda agent, _course, parsed, on_text, learner: agent.tutor(*parsed, on_text, learner=learner),  # type: ignore[attr-defined, misc]
    ),
    "coach.hint": Prompt(
        "course_hint", course=True, tools=False,
        parse=lambda course, value: hint_request({**_course(course), **_fields(value)})[2:],
        run=lambda hint, course, parsed, _on_text, _learner: _answered(hint.hint(course.playground_id, course.version_id, *parsed)),  # type: ignore[attr-defined, union-attr, misc]
    ),
    "coach.follow": Prompt(
        "course_follow", course=True, tools=False,
        parse=lambda course, value: follow_request({**_course(course), **_fields(value)})[2],
        run=lambda follow, course, parsed, _on_text, _learner: _answered(follow.follow(course.playground_id, course.version_id, parsed)),  # type: ignore[attr-defined, union-attr]
    ),
    "coach.diagnose": Prompt(
        "course_diagnose", course=True, tools=False,
        parse=lambda course, value: diagnose_request({**_course(course), **_fields(value)})[2],
        run=lambda diagnose, course, parsed, _on_text, _learner: _answered(diagnose.diagnose(course.playground_id, course.version_id, parsed)),  # type: ignore[attr-defined, union-attr]
    ),
    "coach.debrief": Prompt(
        "course_debrief", course=True, tools=False,
        parse=lambda course, value: debrief_request({**_course(course), **_fields(value)})[2],
        run=lambda debrief, course, parsed, _on_text, _learner: _answered(debrief.debrief(course.playground_id, course.version_id, parsed)),  # type: ignore[attr-defined, union-attr]
    ),
}


def step_request(value: object) -> tuple[str, Prompt, Course | None, object]:
    """信封：{prompt, course, input}；course 是 {playgroundId, versionId} 或 null。input 由 prompt 自己校验。"""
    if not isinstance(value, dict) or set(value) != {"prompt", "course", "input"}:
        raise ValueError("body must contain prompt, course and input")
    name = value["prompt"]
    prompt = PROMPTS.get(name) if isinstance(name, str) else None
    if prompt is None:
        raise ValueError(f"unknown prompt {name!r}")
    course = _course_of(value["course"])
    if prompt.course and course is None:
        raise ValueError(f"prompt {name} needs a course")
    return name, prompt, course, prompt.parse(course, value["input"])


def _course_of(value: object) -> Course | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"playgroundId", "versionId"}:
        raise ValueError("course must contain playgroundId and versionId")
    playground_id, version_id = value["playgroundId"], value["versionId"]
    if not isinstance(playground_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,100}", playground_id) is None:
        raise ValueError("playgroundId is invalid")
    if not isinstance(version_id, str) or not 1 <= len(version_id) <= 200:
        raise ValueError("versionId is invalid")
    return Course(playground_id, version_id)
