"""助教的第 ① 级提示：客户端在该开口的时刻发来代码算好的证据，这里补上课程的规则与函数含义，请模型只写措辞。

第一句由课程给出：学生写的那一层调用的函数，课程的 meanings 说它是什么意思；模型写原因、每行说明和顶部一句话。
校验不过（超长、叫学生去看）就把问题发回去改一次；仍然不过时照样返回，并带上 problems。
"""

from __future__ import annotations

import json
import re
from typing import Protocol

from picopg.server.ai.course import CourseSkillRepository
from picopg.server.ai.model import CourseAiError, ToolModel

SYSTEM = """你是坐在学生旁边的编程助教，学生在自学。规则已经判断出现在该你开口，下面给出依据和事实。

先诊断：透过现象，找出学生没懂的那个概念。常见的是没懂某个函数调用的意思，例如它“把多少个什么，从哪里搬到哪里”。懂了这个概念，学生自己就能改对。

然后只写两句话{first}：
1. concept：说清学生写的那一层里调用的函数是什么意思，也就是调用一次会做成什么事；课程给出了函数含义时就用它，换成学生看得见的说法（盘子、柱子、个数），用 k、X、Y 这样的字母，不用具体数字。
2. reason：根据课程判定和“根源”，用具体的盘子、柱子、行号，说出出错那一行要做什么、为什么走不通、挡住它的东西是哪一行放上去的。
不要写“看看”“想一想”“读读”“试试”“注意”这类叫学生去做什么的话；学生自己会看。不要给出改好的代码。

在“学生写的那一层”讲：学生改的是那一层的代码，问题也要在那一层说清楚，不要钻进更里面的调用。
代码会把这一次调用的具体值贴在那一层的代码上（贴纸），你不用写贴纸。你还要写：
- glosses：[{{ "line": 行号, "text": 这一次调用做成了什么事，以及产生第几步或“还没轮到” }}]，只写“学生写的那一层”里列出的行，每条不超过 25 个字。用具体的个数和柱子说出意思，例如“把 2 个盘从 A 搬到 B”，不要重复贴纸上的写法。
- header：一句话，此刻在执行哪一次调用、它的意思，用具体的值。
- diagnosis：给老师看的一句话，学生没懂什么。

concept 不超过 30 个字，reason 不超过 60 个字。
只输出一个 JSON 对象：{{ "diagnosis", "concept", "reason", "header", "glosses" }}。"""

FILLER = re.compile("看看|想一想|想想|读读|试试|注意")


class CourseHint(Protocol):
    def hint(self, playground_id: str, version_id: str, evidence: str, focus: str | None) -> dict[str, object]: ...


class CourseHintService:
    def __init__(self, model: ToolModel, skills: CourseSkillRepository) -> None:
        self.model = model
        self.skills = skills

    def hint(self, playground_id: str, version_id: str, evidence: str, focus: str | None) -> dict[str, object]:
        skill = self._skill(playground_id, version_id)
        rules = skill.get("rules")
        meanings = skill.get("meanings") if isinstance(skill.get("meanings"), dict) else {}
        signature = next((name for name in meanings if focus is not None and str(name).startswith(f"{focus}(")), None)
        concept = None if signature is None else f"{signature} 就是一句话：{meanings[signature]}。"
        parts = [
            f"## 课程规则\n{rules}" if isinstance(rules, str) else None,
            "## 课程给出的函数含义\n" + "\n".join(f"{name}：{meaning}" for name, meaning in meanings.items()) if meanings else None,
            evidence,
            None if concept is None else f"## 第一句（课程给出，已经定了，不用你写）\n{concept}\n你只写 reason，它接在这一句后面。",
        ]
        first = "" if concept is None else "里的第二句 reason（第一句由课程给出，见证据里的“第一句”）"
        chat: list[dict[str, object]] = [
            {"role": "system", "content": SYSTEM.format(first=first)},
            {"role": "user", "content": "\n\n".join(part for part in parts if part)},
        ]
        hint: dict[str, object] = {}
        problems: list[str] = []
        for _ in range(2):
            content = self.model.step(chat, [], None).get("content")
            if not isinstance(content, str):
                raise CourseAiError("model returned no hint")
            hint = _written(content)
            if concept is not None:
                hint["concept"] = concept
            problems = _problems(hint, concept is None)
            if not problems:
                break
            chat += [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "有这些问题，请改好后重新输出整个 JSON：\n" + "\n".join(f"- {item}" for item in problems)},
            ]
        return {**hint, "problems": problems}

    def _skill(self, playground_id: str, version_id: str) -> dict[str, object]:
        try:
            return self.skills.read(playground_id, version_id)
        except CourseAiError:
            return {}


def _written(content: str) -> dict[str, object]:
    try:
        value = json.loads(content[content.index("{"):content.rindex("}") + 1])
    except ValueError as error:
        raise CourseAiError("model returned an invalid hint") from error
    if not isinstance(value, dict):
        raise CourseAiError("model returned an invalid hint")
    glosses = value.get("glosses")
    return {
        **{key: value[key] for key in ("diagnosis", "concept", "reason", "header") if isinstance(value.get(key), str)},
        "glosses": [
            {"line": item["line"], "text": item["text"]}
            for item in glosses if isinstance(item, dict) and isinstance(item.get("line"), int) and isinstance(item.get("text"), str)
        ] if isinstance(glosses, list) else [],
    }


def _problems(hint: dict[str, object], check_concept: bool) -> list[str]:
    problems = []
    concept, reason = str(hint.get("concept", "")), str(hint.get("reason", ""))
    if not reason:
        problems.append("缺少 reason")
    if check_concept and len(concept) > 30:
        problems.append(f"concept {len(concept)} 字，超过 30")
    if len(reason) > 60:
        problems.append(f"reason {len(reason)} 字，超过 60")
    for gloss in hint.get("glosses", []):  # type: ignore[union-attr]
        if len(gloss["text"]) > 25:
            problems.append(f"第 {gloss['line']} 行说明 {len(gloss['text'])} 字，超过 25")
    for field, text in (("concept", concept), ("reason", reason)):
        if FILLER.search(text):
            problems.append(f"{field} 里有叫学生去做什么的话")
    return problems


def hint_request(value: object) -> tuple[str, str, str, str | None]:
    if not isinstance(value, dict) or set(value) != {"playgroundId", "versionId", "evidence", "focus"}:
        raise ValueError("body must contain playgroundId, versionId, evidence and focus")
    playground_id, version_id, evidence, focus = value["playgroundId"], value["versionId"], value["evidence"], value["focus"]
    if not isinstance(playground_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,100}", playground_id) is None:
        raise ValueError("playgroundId is invalid")
    if not isinstance(version_id, str) or not 1 <= len(version_id) <= 200:
        raise ValueError("versionId is invalid")
    if not isinstance(evidence, str) or not 1 <= len(evidence) <= 30000:
        raise ValueError("evidence is invalid")
    if focus is not None and (not isinstance(focus, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", focus) is None):
        raise ValueError("focus is invalid")
    return playground_id, version_id, evidence, focus
