"""助教的诊断：学生没做对时在后台跑，学生看不到。模型给出最小补丁、缺的概念和给老师的一句话；
补丁对不对由客户端应用后真的运行来验证，没过就把运行结果带回来重试。

提示词是带版本号的文件 prompts/diagnose-v<N>.md，回复里带上 version，评测结果据此记版本。
课程的规则、概念清单、函数含义和参考解放在最前面，同一门课每次都一样，便于模型服务缓存；学生的东西放后面。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

from picopg.server.ai.course import CourseSkillRepository, OneSkill
from picopg.server.ai.model import CourseAiError, ToolModel

VERSION = "diagnose-v2"
PROMPT = (Path(__file__).with_name("prompts") / f"{VERSION}.md").read_text(encoding="utf-8")


class CourseDiagnose(Protocol):
    def diagnose(self, playground_id: str, version_id: str, request: dict[str, object]) -> dict[str, object]: ...


class CourseDiagnoseService:
    def __init__(self, model: ToolModel, skills: CourseSkillRepository) -> None:
        self.model = model
        self.skills = skills

    def diagnose(self, playground_id: str, version_id: str, request: dict[str, object]) -> dict[str, object]:
        skill = self._skill(playground_id, version_id)
        concepts = [item for item in skill.get("concepts", []) if isinstance(item, str)] if isinstance(skill.get("concepts"), list) else []
        chat: list[dict[str, object]] = [
            {"role": "system", "content": "\n\n".join(part for part in (PROMPT, _course(skill, concepts, request.get("reference"))) if part)},
            {"role": "user", "content": _student(request)},
        ]
        problems: list[str] = []
        for _ in range(2):
            content = self.model.step(chat, [], None).get("content")
            if not isinstance(content, str):
                raise CourseAiError("model returned no diagnosis")
            proposal, problems = _proposal(content, concepts)
            if not problems:
                return {**proposal, "version": VERSION}
            chat += [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "有这些问题，请改好后重新输出整个 JSON：\n" + "\n".join(f"- {item}" for item in problems)},
            ]
        raise CourseAiError("model returned an invalid diagnosis: " + "；".join(problems))

    def _skill(self, playground_id: str, version_id: str) -> dict[str, object]:
        try:
            return self.skills.read(playground_id, version_id)
        except CourseAiError:
            return {}


def _course(skill: dict[str, object], concepts: list[str], reference: object) -> str:
    meanings = skill.get("meanings") if isinstance(skill.get("meanings"), dict) else {}
    parts = [
        f"## 课程规则\n{skill['rules']}" if isinstance(skill.get("rules"), str) else None,
        "## 课程的概念清单\n" + "、".join(concepts) if concepts else None,
        "## 课程给出的函数含义\n" + "\n".join(f"{name}：{meaning}" for name, meaning in meanings.items()) if meanings else None,
        f"## 参考解（课程认为对的一种写法；学生可以有别的写法）\n```python\n{reference}\n```" if isinstance(reference, str) and reference else None,
    ]
    return "\n\n".join(part for part in parts if part)


def _student(request: dict[str, object]) -> str:
    deleted = request.get("deleted") or []
    rejected = request.get("rejected") or []
    parts = [
        str(request["evidence"]),
        "## 学生删掉过的代码（旧的在前）\n" + "\n".join(f"- `{line}`" for line in deleted) if deleted else None,
        "## 之前的诊断没通过验证（补丁应用后运行的结果）\n" + "\n".join(
            f"- 补丁 {json.dumps(item['proposal']['patch'], ensure_ascii=False)}：{item['result']}" for item in rejected  # type: ignore[index]
        ) if rejected else None,
    ]
    return "\n\n".join(part for part in parts if part)


def _proposal(content: str, concepts: list[str]) -> tuple[dict[str, object], list[str]]:
    try:
        value = json.loads(content[content.index("{"):content.rindex("}") + 1])
    except ValueError:
        return {}, ["不是一个 JSON 对象"]
    if not isinstance(value, dict):
        return {}, ["不是一个 JSON 对象"]
    patch = value.get("patch")
    problems = []
    edits = []
    if not isinstance(patch, list) or not patch:
        problems.append("patch 要是非空的数组")
    else:
        for item in patch:
            if not isinstance(item, dict) or not isinstance(item.get("line"), int) or not isinstance(item.get("old"), str) or not isinstance(item.get("text"), str):
                problems.append("patch 里每一处都要是 { line: 整数, old: 字符串, text: 字符串 }")
                break
            edits.append({"line": item["line"], "old": item["old"], "text": item["text"]})
    concept, note = value.get("concept"), value.get("note")
    if concepts and concept not in concepts:
        problems.append(f"concept 只能是 {'、'.join(concepts)} 里的一个")
    if not isinstance(note, str):
        problems.append("缺少 note")
    return {"patch": edits, "concept": concept if isinstance(concept, str) else "", "note": note if isinstance(note, str) else ""}, problems


def diagnose_request(value: object) -> tuple[str, str, dict[str, object]]:
    keys = {"playgroundId", "versionId", "evidence", "reference", "deleted", "rejected"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("body must contain " + ", ".join(sorted(keys)))
    playground_id, version_id, evidence, reference = value["playgroundId"], value["versionId"], value["evidence"], value["reference"]
    if not isinstance(playground_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,100}", playground_id) is None:
        raise ValueError("playgroundId is invalid")
    if not isinstance(version_id, str) or not 1 <= len(version_id) <= 200:
        raise ValueError("versionId is invalid")
    if not isinstance(evidence, str) or not 1 <= len(evidence) <= 30000:
        raise ValueError("evidence is invalid")
    if reference is not None and (not isinstance(reference, str) or len(reference) > 20000):
        raise ValueError("reference is invalid")
    deleted, rejected = value["deleted"], value["rejected"]
    if not isinstance(deleted, list) or len(deleted) > 50 or not all(isinstance(line, str) and len(line) <= 500 for line in deleted):
        raise ValueError("deleted is invalid")
    if not isinstance(rejected, list) or len(rejected) > 5 or not all(
        isinstance(item, dict) and isinstance(item.get("result"), str) and isinstance(item.get("proposal"), dict)
        and isinstance(item["proposal"].get("patch"), list) for item in rejected
    ):
        raise ValueError("rejected is invalid")
    return playground_id, version_id, {"evidence": evidence, "reference": reference, "deleted": deleted, "rejected": rejected}


def main() -> None:
    """离线评测用：python -m picopg.server.ai.diagnose <skill.json> < 请求 JSON，模型取 PICOPG_AI_* 环境变量。"""
    import sys

    from picopg.server.config import diagnose_model

    model = diagnose_model()
    if model is None:
        raise SystemExit("没有配置模型：PICOPG_AI_BASE_URL / PICOPG_AI_MODEL / PICOPG_AI_API_KEY")
    skill = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    service = CourseDiagnoseService(model, OneSkill(skill))  # type: ignore[arg-type]
    diagnosis = service.diagnose(str(skill.get("course")), "offline", json.load(sys.stdin))
    print(json.dumps({**diagnosis, "model": getattr(model, "answered", None) or getattr(model, "model", None)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
