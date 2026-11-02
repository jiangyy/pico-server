"""学生画像：对学习证据的纯函数汇总。规则确定、可以随时重算，不调用模型。

证据只记发生了什么；这里按知识地图把它们汇总到概念上。强证据（练习批改、回答判定、预测题）决定掌握状态，
弱证据（看过讲稿、提过问题）只说明接触过。概念 id 从不复用，所以旧证据按地图的 migrations 换算到现在的 id。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# 掌握后的复习间隔（天）：每多通过一次，间隔拉长一档。
REVIEW_DAYS = (1, 3, 7, 14, 30)
MISCONCEPTIONS_KEPT = 8
OPEN_KEPT = 3


class Concepts:
    """知识地图里的概念：换算旧 id，按别名把学生原话对应到概念。没有地图时原样使用证据里的 id。"""

    def __init__(self, knowledge: Mapping[str, object] | None) -> None:
        self.version = int(knowledge.get("version", 0)) if knowledge else 0
        items = [item for item in (knowledge or {}).get("concepts", []) if isinstance(item, Mapping)]
        self.labels = {str(item["id"]): str(item.get("label", item["id"])) for item in items}
        self.requires = {str(item["id"]): [str(other) for other in item.get("requires", [])] for item in items}
        self.courses = {str(key): [str(item) for item in value] for key, value in (knowledge or {}).get("courses", {}).items()}
        migrations = (knowledge or {}).get("migrations", {})
        self.renames = [migrations[key] for key in sorted(migrations, key=int)]
        # 一个字的别名（如 =、%）太容易误中，不用来匹配。
        self.words = [
            (word.lower(), str(item["id"]))
            for item in items for word in (str(item.get("label", "")), *item.get("aliases", [])) if len(word) >= 2
        ]

    def id(self, raw: object) -> str | None:
        if not isinstance(raw, str) or not raw:
            return None
        if not self.labels:
            return raw
        for renames in self.renames:
            raw = renames.get(raw, raw)
        return raw if raw in self.labels else None

    def ids(self, raws: object) -> list[str]:
        found = [self.id(raw) for raw in raws] if isinstance(raws, list) else []
        return list(dict.fromkeys(item for item in found if item is not None))

    def mentioned(self, text: str) -> list[str]:
        lowered = text.lower()
        return list(dict.fromkeys(concept for word, concept in self.words if word in lowered))

    def label(self, concept: str) -> str:
        return self.labels.get(concept, concept)


@dataclass
class _Concept:
    seen: int = 0
    questions: int = 0
    practice: Counter[str] = field(default_factory=Counter)
    answers: Counter[str] = field(default_factory=Counter)
    predictions: list[bool] = field(default_factory=list)
    # 强证据按时间排列：(结论, 时间, 是否来自练习或回答)。
    outcomes: list[tuple[str, str | None, bool]] = field(default_factory=list)
    misconceptions: Counter[str] = field(default_factory=Counter)


def profile_of(evidence: Iterable[Mapping[str, object]], knowledge: Mapping[str, object] | None) -> dict[str, object]:
    """evidence 是 learning.evidence.recorded 记录，按发生顺序排列。"""
    concepts = Concepts(knowledge)
    states: dict[str, _Concept] = {}
    misconceptions: dict[str, dict[str, object]] = {}
    steps: dict[tuple[str, str, int], list[str]] = {}
    open_practice: dict[str, dict[str, object]] = {}
    last_active: str | None = None
    habits = {
        "questions": 0, "explains": 0, "answers": Counter(), "predictions": Counter(),
        "passedPractice": 0, "attemptsToPass": [], "askedBeforeSubmit": [], "errors": Counter(),
    }

    def state(concept: str) -> _Concept:
        return states.setdefault(concept, _Concept())

    def judged(found: list[str], verdict: object, at: str | None, data: Mapping[str, object], course: str, kind: str) -> None:
        if verdict not in ("pass", "partial", "fail"):
            return
        for concept in found:
            item = state(concept)
            (item.practice if kind == "practice" else item.answers)[verdict] += 1
            item.outcomes.append((str(verdict), at, True))
        misconception = data.get("misconception")
        if isinstance(misconception, str) and misconception.strip() and verdict != "pass":
            entry = misconceptions.setdefault(misconception, {"id": misconception, "concepts": found, "count": 0, "course": course, "last": at})
            entry.update(count=int(entry["count"]) + 1, course=course, last=at)
            for concept in found:
                state(concept).misconceptions[misconception] += 1

    for record in evidence:
        payload = record.get("payload")
        if not isinstance(payload, Mapping) or payload.get("schema") != "session.activity" or payload.get("version") != 1:
            continue
        data = payload.get("data")
        if not isinstance(data, Mapping):
            continue
        at = record.get("recordedAt") if isinstance(record.get("recordedAt"), str) else None
        last_active = max(last_active or "", at or "") or None
        subject = record.get("subject") if isinstance(record.get("subject"), Mapping) else {}
        course = str(subject.get("playgroundId", ""))
        session = str((record.get("source") or {}).get("session", "")) if isinstance(record.get("source"), Mapping) else ""
        kind = data.get("kind")
        found = concepts.ids(data.get("concepts"))
        if kind == "script.step":
            steps[(session, str(data.get("script")), int(data.get("step", -1)))] = found
            for concept in found:
                state(concept).seen += 1
        elif kind == "script.prediction" and isinstance(data.get("correct"), bool):
            habits["predictions"]["correct" if data["correct"] else "wrong"] += 1
            for concept in found:
                state(concept).predictions.append(data["correct"])
                state(concept).outcomes.append(("pass" if data["correct"] else "fail", at, False))
        elif kind == "ai.question":
            habits["explains" if data.get("explain") is True else "questions"] += 1
            # 讲稿正在讲的那一步说明问题在问什么；不在讲稿里时按学生的原话对应概念。
            where = data.get("script") if isinstance(data.get("script"), Mapping) else None
            context = steps.get((session, str(where.get("id")), int(where.get("step", -1)))) if where else None
            for concept in context or concepts.mentioned(str(data.get("text", ""))):
                state(concept).questions += 1
        elif kind == "ai.answer":
            habits["answers"][str(data.get("verdict", "unjudged"))] += 1
            judged(found, data.get("verdict"), at, data, course, "answer")
        elif kind == "ai.practice" and data.get("stage") == "assigned":
            open_practice[str(data.get("task", ""))] = {"task": data.get("task"), "course": course, "concepts": found}
        elif kind == "ai.practice" and data.get("stage") == "graded":
            judged(found, data.get("verdict"), at, data, course, "practice")
            if data.get("verdict") == "pass":
                open_practice.pop(str(data.get("task", "")), None)
                habits["passedPractice"] += 1
                if isinstance(data.get("attempt"), int):
                    habits["attemptsToPass"].append(data["attempt"])
            if isinstance(data.get("asked"), int):
                habits["askedBeforeSubmit"].append(data["asked"])
        elif kind == "run.finished" and data.get("status") == "error":
            error = data.get("error")
            if isinstance(error, Mapping) and isinstance(error.get("type"), str):
                habits["errors"][error["type"]] += 1

    rows = [_row(concept, item, concepts) for concept, item in states.items()]
    rows.sort(key=lambda row: (("shaky", "learning", "solid").index(str(row["status"])), str(row["concept"])))
    kept = sorted(misconceptions.values(), key=lambda entry: (-int(entry["count"]), str(entry["last"] or "")))[:MISCONCEPTIONS_KEPT]
    return {
        "version": 1,
        "knowledge": concepts.version,
        "lastActive": last_active,
        "concepts": rows,
        "misconceptions": kept,
        "habits": {
            "questions": habits["questions"],
            "explains": habits["explains"],
            "answers": dict(habits["answers"]),
            "predictions": dict(habits["predictions"]),
            "passedPractice": habits["passedPractice"],
            "attemptsToPass": _mean(habits["attemptsToPass"]),
            "askedBeforeSubmit": _mean(habits["askedBeforeSubmit"]),
            "errors": dict(habits["errors"].most_common(5)),
        },
        "open": list(open_practice.values())[-OPEN_KEPT:],
    }


def _row(concept: str, item: _Concept, concepts: Concepts) -> dict[str, object]:
    passes = [at for outcome, at, _ in item.outcomes if outcome == "pass"]
    last = item.outcomes[-1][0] if item.outcomes else None
    predicted = len(item.predictions)
    if last in ("partial", "fail") \
            or (predicted >= 2 and sum(item.predictions) / predicted < 0.5) \
            or any(count >= 2 for count in item.misconceptions.values()):
        status = "shaky"
    # 掌握：最近两次都通过，其中至少一次是练习或回答；预测题是选择题，单靠它不算。
    elif [outcome for outcome, _, _ in item.outcomes[-2:]] == ["pass", "pass"] \
            and any(judged for _, _, judged in item.outcomes[-2:]):
        status = "solid"
    else:
        status = "learning"
    row: dict[str, object] = {"concept": concept, "label": concepts.label(concept), "status": status}
    for key, value in (("seen", item.seen), ("questions", item.questions)):
        if value:
            row[key] = value
    if item.practice:
        row["practice"] = dict(item.practice)
    if item.answers:
        row["answers"] = dict(item.answers)
    if predicted:
        row["predictions"] = {"correct": sum(item.predictions), "total": predicted}
    if item.misconceptions:
        row["misconceptions"] = [name for name, _ in item.misconceptions.most_common(3)]
    times = [at for _, at, _ in item.outcomes if at is not None]
    if times:
        row["lastPracticed"] = times[-1]
    if status == "solid" and passes[-1] is not None:
        days = REVIEW_DAYS[min(len(passes) - 2, len(REVIEW_DAYS) - 1)]
        row["reviewDue"] = (_parse(passes[-1]) + timedelta(days=days)).isoformat()
    return row


ACTIVITY_KINDS = ("ai.question", "ai.answer", "ai.practice", "script.prediction")


def activity_of(evidence: Iterable[Mapping[str, object]], limit: int) -> list[dict[str, object]]:
    """学生主动的交互：提问、回答与判定、练习的布置与批改、预测题；最近的在前。"""
    found = []
    for record in evidence:
        payload = record.get("payload")
        data = payload.get("data") if isinstance(payload, Mapping) and payload.get("schema") == "session.activity" else None
        if not isinstance(data, Mapping) or data.get("kind") not in ACTIVITY_KINDS:
            continue
        subject = record.get("subject") if isinstance(record.get("subject"), Mapping) else {}
        found.append({"at": record.get("recordedAt"), "course": subject.get("playgroundId"), **data})
    return found[::-1][:limit]


def due(profile: Mapping[str, object], now: datetime) -> list[str]:
    """已经掌握、到了复习时间的概念。"""
    return [
        str(row["concept"]) for row in profile.get("concepts", [])
        if isinstance(row, Mapping) and isinstance(row.get("reviewDue"), str) and _parse(row["reviewDue"]) <= now
    ]


def for_course(profile: Mapping[str, object], course: str, knowledge: Mapping[str, object] | None, now: datetime) -> dict[str, object]:
    """课程里的 AI 只需要本课的概念、本课概念不牢的先修、本课的误区与学习习惯。"""
    concepts = Concepts(knowledge)
    own = set(concepts.courses.get(course, []))
    rows = {str(row["concept"]): row for row in profile.get("concepts", []) if isinstance(row, Mapping)}
    weak_requires = {
        required for concept in own for required in concepts.requires.get(concept, [])
        if required not in own and rows.get(required, {}).get("status") == "shaky"
    }
    return {
        "concepts": [row for concept, row in rows.items() if concept in own or concept in weak_requires],
        "misconceptions": [
            entry for entry in profile.get("misconceptions", [])
            if isinstance(entry, Mapping) and (entry.get("course") == course or own & set(entry.get("concepts", [])))
        ],
        "habits": profile.get("habits", {}),
        "due": due(profile, now),
        "open": [entry for entry in profile.get("open", []) if isinstance(entry, Mapping) and entry.get("course") == course],
    }


def _mean(values: list[int]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
