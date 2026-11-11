"""做对之后的复盘：客户端发来学生做对的代码和代码跑出来的事实（对拍、极端用例），请模型写思路判断、
按他思路更干净的写法、数学抽象与更好的解法，以及边讲边写出代码的讲解（lectures）。代码由客户端运行验证，不过就把结果发回来重写。
提示词是带版本号的文件 prompts/review-v<N>.md。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Protocol

from picopg.server.ai.course import CourseSkillRepository, OneSkill
from picopg.server.ai.model import CourseAiError, ToolModel

VERSION = os.environ.get("PICOPG_REVIEW_PROMPT", "review-v2")
PROMPT = (Path(__file__).with_name("prompts") / f"{VERSION}.md").read_text(encoding="utf-8")
TEXTS = ("idea", "right", "cleaner", "cleaner_why", "math", "better", "complexity", "edges")


class CourseReview(Protocol):
    def review(self, playground_id: str, version_id: str, request: dict[str, object]) -> dict[str, object]: ...


class CourseReviewService:
    def __init__(self, model: ToolModel, skills: CourseSkillRepository | OneSkill) -> None:
        self.model = model
        self.skills = skills

    def review(self, playground_id: str, version_id: str, request: dict[str, object]) -> dict[str, object]:
        """request：source 他做对的代码，facts 题目和代码算出的事实，reference 参考解或 null，rejected 上一版没验证过的代码和运行结果。"""
        skill = self._skill(playground_id, version_id)
        parts = [
            f"## 课程规则\n{skill['rules']}" if isinstance(skill.get("rules"), str) else None,
            f"## 他做对的代码\n```python\n{request['source']}\n```",
            f"## 代码跑出来的事实\n{request['facts']}",
            f"## 课程的参考解\n```python\n{request['reference']}\n```" if isinstance(request.get("reference"), str) else None,
        ]
        chat: list[dict[str, object]] = [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": "\n\n".join(part for part in parts if part)},
        ]
        rejected = request.get("rejected")
        if isinstance(rejected, dict):
            chat += [
                {"role": "assistant", "content": json.dumps(rejected["review"], ensure_ascii=False)},
                {"role": "user", "content": f"代码跑过了，有这些问题，请改好后重新输出整个 JSON：\n{rejected['result']}"},
            ]
        problems: list[str] = []
        for _ in range(2):
            content = self.model.step(chat, [], None).get("content")
            if not isinstance(content, str):
                raise CourseAiError("model returned no review")
            review, problems = _review(content)
            if not problems:
                return {**review, "version": VERSION}
            chat += [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "有这些问题，请改好后重新输出整个 JSON：\n" + "\n".join(f"- {item}" for item in problems)},
            ]
        raise CourseAiError("model returned an invalid review: " + "；".join(problems))

    def _skill(self, playground_id: str, version_id: str) -> dict[str, object]:
        try:
            return self.skills.read(playground_id, version_id)
        except CourseAiError:
            return {}


def _review(content: str) -> tuple[dict[str, object], list[str]]:
    try:
        value = json.loads(content[content.index("{"):content.rindex("}") + 1])
    except ValueError:
        return {}, ["输出不是一个 JSON 对象"]
    if not isinstance(value, dict):
        return {}, ["输出不是一个 JSON 对象"]
    problems = [f"缺少 {key}" for key in TEXTS if not isinstance(value.get(key), str) or not value[key].strip()]
    better = value.get("better_code")
    if better is not None and not isinstance(better, str):
        problems.append("better_code 要么是代码，要么是 null")
    review = {key: _code(value[key]) if key == "cleaner" else value[key].strip() for key in TEXTS if isinstance(value.get(key), str)}
    review["better_code"] = _code(better) if isinstance(better, str) and better.strip() else None
    lectures, wrong = _lectures(value.get("lectures"))
    review["lectures"] = lectures
    return review, problems + wrong


def _lectures(value: object) -> tuple[list[dict[str, object]], list[str]]:
    """每份 { title, steps: [{ title, beats: [{ say, write? }] }] }，最后一句要写出完整解法。"""
    if not isinstance(value, list) or not 1 <= len(value) <= 2:
        return [], ["lectures 要有 1–2 份讲解"]
    lectures: list[dict[str, object]] = []
    problems: list[str] = []
    for number, lecture in enumerate(value, 1):
        steps = lecture.get("steps") if isinstance(lecture, dict) else None
        if not isinstance(lecture, dict) or not isinstance(lecture.get("title"), str) or not isinstance(steps, list) or not steps:
            problems.append(f"第 {number} 份讲解要有 title 和 steps")
            continue
        shaped = []
        for step in steps:
            beats = step.get("beats") if isinstance(step, dict) else None
            if not isinstance(beats, list) or not beats or not all(isinstance(beat, dict) and isinstance(beat.get("say"), str) for beat in beats):
                problems.append(f"第 {number} 份讲解的每一步要有 beats，每句有 say")
                break
            shaped.append({
                "title": step.get("title") if isinstance(step.get("title"), str) else None,
                "beats": [{"say": beat["say"], **({"write": _code(beat["write"])} if isinstance(beat.get("write"), str) and beat["write"].strip() else {})} for beat in beats],
            })
        else:
            if "write" not in shaped[-1]["beats"][-1]:
                problems.append(f"第 {number} 份讲解的最后一句要写出完整解法（write）")
            lectures.append({"title": lecture["title"], "steps": shaped})
    return lectures, problems


def _code(text: str) -> str:
    """模型常把代码包在 ``` 里。"""
    fenced = re.search(r"```(?:python)?\n(.*?)```", text, re.S)
    return (fenced.group(1) if fenced else text).strip("\n") + "\n"


def review_request(value: object) -> tuple[str, str, dict[str, object]]:
    if not isinstance(value, dict) or not {"playgroundId", "versionId", "source", "facts"} <= set(value) <= {"playgroundId", "versionId", "source", "facts", "reference", "rejected"}:
        raise ValueError("body must contain playgroundId, versionId, source and facts")
    playground_id, version_id = value["playgroundId"], value["versionId"]
    if not isinstance(playground_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,100}", playground_id) is None:
        raise ValueError("playgroundId is invalid")
    if not isinstance(version_id, str) or not 1 <= len(version_id) <= 200:
        raise ValueError("versionId is invalid")
    for key, limit in (("source", 20000), ("facts", 20000)):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= limit:
            raise ValueError(f"{key} is invalid")
    if value.get("reference") is not None and (not isinstance(value["reference"], str) or len(value["reference"]) > 20000):
        raise ValueError("reference is invalid")
    rejected = value.get("rejected")
    if rejected is not None and (not isinstance(rejected, dict) or set(rejected) != {"review", "result"} or not isinstance(rejected["result"], str)):
        raise ValueError("rejected is invalid")
    return playground_id, version_id, {key: value.get(key) for key in ("source", "facts", "reference", "rejected")}


def main() -> None:
    """离线评测用：python -m picopg.server.ai.review <skill.json> < { "source", "facts", "reference", "rejected" }，模型取 PICOPG_AI_* 环境变量。"""
    import sys

    from picopg.server.config import load_course_hint

    loaded = load_course_hint()
    if loaded is None:
        raise SystemExit("没有配置模型：PICOPG_AI_BASE_URL / PICOPG_AI_MODEL / PICOPG_AI_API_KEY")
    skill = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    service = CourseReviewService(loaded.model, OneSkill(skill))  # type: ignore[attr-defined, arg-type]
    review = service.review(str(skill.get("course")), "offline", json.load(sys.stdin))
    model = loaded.model  # type: ignore[attr-defined]
    print(json.dumps({**review, "model": getattr(model, "answered", None) or getattr(model, "model", None)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
