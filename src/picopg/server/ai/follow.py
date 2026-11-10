"""助教的“跟”：一条从学生打开题目开始的线程，每次尝试之后模型写一份思路笔记，学生看不到。

客户端每次带上整条线程（尝试消息由客户端的代码生成，笔记是之前的回复），这里补上固定前缀：提示词、课程规则、题目、
概念清单、函数含义、参考解。前缀同一门课每次都一样，便于模型服务缓存。
这里只校验笔记的形状；行号、尝试号存不存在由客户端对照尝试检查。提示词是带版本号的文件 prompts/follow-v<N>.md。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

from picopg.server.ai.course import CourseSkillRepository, OneSkill
from picopg.server.ai.model import CourseAiError, ToolModel

VERSION = "follow-v3"
PROMPT = (Path(__file__).with_name("prompts") / f"{VERSION}.md").read_text(encoding="utf-8")
GAPS = ("plan", "expression", "execution")
ACTIONS = ("edit", "run", "debug", "stop")


class CourseFollow(Protocol):
    def follow(self, playground_id: str, version_id: str, request: dict[str, object]) -> dict[str, object]: ...


class CourseFollowService:
    def __init__(self, model: ToolModel, skills: CourseSkillRepository | OneSkill) -> None:
        self.model = model
        self.skills = skills

    def follow(self, playground_id: str, version_id: str, request: dict[str, object]) -> dict[str, object]:
        skill = self._skill(playground_id, version_id)
        chat: list[dict[str, object]] = [
            {"role": "system", "content": "\n\n".join(part for part in (PROMPT, _course(skill, request.get("reference"))) if part)},
            *({"role": turn["role"], "content": turn["content"]} for turn in request["thread"]),  # type: ignore[union-attr, index]
        ]
        problems: list[str] = []
        for _ in range(2):
            content = self.model.step(chat, [], None).get("content")
            if not isinstance(content, str):
                raise CourseAiError("model returned no note")
            note, problems = _note(content)
            if not problems:
                return {**note, "version": VERSION}
            chat += [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "这份笔记有这些问题，请改好后重新输出整个 JSON：\n" + "\n".join(f"- {item}" for item in problems)},
            ]
        raise CourseAiError("model returned an invalid note: " + "；".join(problems))

    def _skill(self, playground_id: str, version_id: str) -> dict[str, object]:
        try:
            return self.skills.read(playground_id, version_id)
        except CourseAiError:
            return {}


def _course(skill: dict[str, object], reference: object) -> str:
    meanings = skill.get("meanings") if isinstance(skill.get("meanings"), dict) else {}
    concepts = [item for item in skill.get("concepts", []) if isinstance(item, str)] if isinstance(skill.get("concepts"), list) else []
    parts = [
        f"## 题目\n{skill['teaches']}" if isinstance(skill.get("teaches"), str) else None,
        f"## 课程规则\n{skill['rules']}" if isinstance(skill.get("rules"), str) else None,
        "## 课程的概念清单\n" + "、".join(concepts) if concepts else None,
        "## 课程给出的函数含义\n" + "\n".join(f"{name}：{meaning}" for name, meaning in meanings.items()) if meanings else None,
        f"## 参考解（课程认为对的一种写法；学生可以有自己的写法，按他的走）\n```python\n{reference}\n```" if isinstance(reference, str) and reference else None,
    ]
    return "\n\n".join(part for part in parts if part)


def _note(content: str) -> tuple[dict[str, object], list[str]]:
    try:
        value = json.loads(content[content.index("{"):content.rindex("}") + 1])
    except ValueError:
        return {}, ["不是一个 JSON 对象"]
    if not isinstance(value, dict):
        return {}, ["不是一个 JSON 对象"]
    problems = []
    belief, nxt, want, basis = value.get("belief"), value.get("next"), value.get("want"), value.get("basis")
    numbers = lambda items: isinstance(items, list) and all(isinstance(item, int) for item in items)  # noqa: E731
    if not isinstance(value.get("doing"), str):
        problems.append("缺少 doing")
    if not isinstance(belief, dict) or not isinstance(belief.get("text"), str) or not numbers(belief.get("lines")) or not numbers(belief.get("steps")):
        problems.append("belief 要是 { text, lines: [整数], steps: [整数] }")
    if value.get("question") is not None and not isinstance(value.get("question"), str):
        problems.append("question 要是字符串或 null")
    if value.get("gap") not in GAPS:
        problems.append(f"gap 只能是 {'、'.join(GAPS)}")
    if not isinstance(nxt, dict) or nxt.get("action") not in ACTIONS or not numbers(nxt.get("lines")):
        problems.append(f"next 要是 {{ action: {'|'.join(ACTIONS)}, lines: [整数] }}")
    if not isinstance(want, dict) or want.get("say") not in ("wait", "speak") or not isinstance(want.get("why"), str):
        problems.append("want 要是 { say: wait|speak, why }")
    if not isinstance(basis, list) or not all(
        isinstance(item, dict) and isinstance(item.get("attempt"), int) and (item.get("line") is None or isinstance(item.get("line"), int)) for item in basis
    ):
        problems.append("basis 要是 [{ attempt: 整数, line: 整数或 null }]")
    lacks = value.get("lacks", [])
    if not isinstance(lacks, list) or len(lacks) > 3 or not all(
        isinstance(item, dict) and isinstance(item.get("what"), str) and isinstance(item.get("seen"), list)
        and all(isinstance(piece, str) for piece in item["seen"]) for item in lacks
    ):
        problems.append("lacks 要是最多 3 条 [{ what, seen: [代码片段] }]")
    if problems:
        return {}, problems
    return {
        "lacks": [{"what": item["what"], "seen": item["seen"]} for item in lacks],
        "doing": value["doing"], "question": value.get("question"), "gap": value["gap"],
        "belief": {"text": belief["text"], "lines": belief["lines"], "steps": belief["steps"]},  # type: ignore[index]
        "next": {"action": nxt["action"], "lines": nxt["lines"]},  # type: ignore[index]
        "want": {"say": want["say"], "why": want["why"]},  # type: ignore[index]
        "basis": [{"attempt": item["attempt"], "line": item.get("line")} for item in basis],  # type: ignore[union-attr]
    }, []


def follow_request(value: object) -> tuple[str, str, dict[str, object]]:
    keys = {"playgroundId", "versionId", "reference", "thread"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("body must contain " + ", ".join(sorted(keys)))
    playground_id, version_id, reference, thread = value["playgroundId"], value["versionId"], value["reference"], value["thread"]
    if not isinstance(playground_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,100}", playground_id) is None:
        raise ValueError("playgroundId is invalid")
    if not isinstance(version_id, str) or not 1 <= len(version_id) <= 200:
        raise ValueError("versionId is invalid")
    if reference is not None and (not isinstance(reference, str) or len(reference) > 20000):
        raise ValueError("reference is invalid")
    if not isinstance(thread, list) or not 1 <= len(thread) <= 100 or not all(
        isinstance(turn, dict) and set(turn) == {"role", "content"} and turn["role"] == ("user" if index % 2 == 0 else "assistant")
        and isinstance(turn["content"], str) and 1 <= len(turn["content"]) <= 20000
        for index, turn in enumerate(thread)
    ) or len(thread) % 2 == 0:
        raise ValueError("thread must alternate user and assistant turns, starting and ending with user")
    return playground_id, version_id, {"reference": reference, "thread": thread}


def main() -> None:
    """离线评测用：python -m picopg.server.ai.follow <skill.json> < { "reference", "thread" }，模型取 PICOPG_AI_* 环境变量。"""
    import sys

    from picopg.server.config import follow_model

    model = follow_model()
    if model is None:
        raise SystemExit("没有配置模型：PICOPG_AI_BASE_URL / PICOPG_AI_MODEL / PICOPG_AI_API_KEY")
    skill = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    note = CourseFollowService(model, OneSkill(skill)).follow(str(skill.get("course")), "offline", json.load(sys.stdin))
    print(json.dumps({**note, "model": getattr(model, "answered", None) or getattr(model, "model", None)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
