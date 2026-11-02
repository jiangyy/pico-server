"""学生画像：对学习证据的纯函数汇总。规则确定、可以随时重算，不调用模型。

证据只记发生了什么；这里按知识地图把它们汇总到概念上。强证据（练习批改、回答判定、预测题）决定掌握状态，
弱证据（看过讲稿、提过问题）只说明接触过。概念 id 从不复用，所以旧证据按地图的 migrations 换算到现在的 id。
"""

from __future__ import annotations

from bisect import insort
from collections import Counter, OrderedDict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# 掌握后的复习间隔（天）：每多通过一次，间隔拉长一档。
REVIEW_DAYS = (1, 3, 7, 14, 30)
MISCONCEPTIONS_KEPT = 8
OPEN_KEPT = 3
# 累加器的上限：每个概念最近几次强证据、记住的讲稿步骤、在跟踪的练习、给老师看的交互。
OUTCOMES_KEPT = 10
STEPS_KEPT = 200
OPEN_TRACKED = 50
ACTIVITY_KEPT = 100
ACTIVITY_KINDS = ("ai.question", "ai.answer", "ai.practice", "script.prediction")


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
    predicted: int = 0
    predicted_right: int = 0
    passes: int = 0
    last_pass: str | None = None
    # 最近几次强证据，按时间排列：(时间, 结论, 是否来自练习或回答)。只留最近 OUTCOMES_KEPT 条。
    outcomes: list[tuple[str, str, bool]] = field(default_factory=list)
    misconceptions: Counter[str] = field(default_factory=Counter)

    def outcome(self, verdict: str, at: str | None, judged: bool) -> None:
        # 多台设备的证据到达顺序不定：按时间插入，同一时间的保持到达顺序。
        insort(self.outcomes, (at or "", verdict, judged), key=lambda item: item[0])
        del self.outcomes[:-OUTCOMES_KEPT]
        if verdict == "pass":
            self.passes += 1
            if at is not None and (self.last_pass is None or at >= self.last_pass):
                self.last_pass = at


class Learner:
    """一个学生的画像累加器：证据一条条喂进来，占用的内存有上限，随时可以取出画像。

    同一台设备的证据要按日志顺序喂（讲稿步骤与随后的提问、练习的布置与批改有先后）；不同设备之间的先后无关。
    """

    def __init__(self, knowledge: Mapping[str, object] | None) -> None:
        self.concepts = Concepts(knowledge)
        self.states: dict[str, _Concept] = {}
        self.misconceptions: dict[str, dict[str, object]] = {}
        self.steps: OrderedDict[tuple[str, str, int], list[str]] = OrderedDict()
        self.open_practice: OrderedDict[str, dict[str, object]] = OrderedDict()
        self.activity: deque[dict[str, object]] = deque(maxlen=ACTIVITY_KEPT)
        self.last_active: str | None = None
        self.counts: Counter[str] = Counter()
        self.answers: Counter[str] = Counter()
        self.predictions: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self.attempts = [0, 0]
        self.asked = [0, 0]

    def state(self, concept: str) -> _Concept:
        return self.states.setdefault(concept, _Concept())

    def feed(self, record: Mapping[str, object]) -> None:
        payload = record.get("payload")
        if not isinstance(payload, Mapping) or payload.get("schema") != "session.activity" or payload.get("version") != 1:
            return
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return
        at = record.get("recordedAt") if isinstance(record.get("recordedAt"), str) else None
        self.last_active = max(self.last_active or "", at or "") or None
        subject = record.get("subject") if isinstance(record.get("subject"), Mapping) else {}
        course = str(subject.get("playgroundId", ""))
        source = record.get("source")
        session = str(source.get("session", "")) if isinstance(source, Mapping) else ""
        kind = data.get("kind")
        if kind in ACTIVITY_KINDS:
            self.activity.append({"at": at, "course": course or None, **data})
        found = self.concepts.ids(data.get("concepts"))
        if kind == "script.step":
            self._remember(self.steps, (session, str(data.get("script")), _int(data.get("step"))), found, STEPS_KEPT)
            for concept in found:
                self.state(concept).seen += 1
        elif kind == "script.prediction" and isinstance(data.get("correct"), bool):
            self.predictions["correct" if data["correct"] else "wrong"] += 1
            for concept in found:
                item = self.state(concept)
                item.predicted += 1
                item.predicted_right += data["correct"]
                item.outcome("pass" if data["correct"] else "fail", at, False)
        elif kind == "ai.question":
            self.counts["explains" if data.get("explain") is True else "questions"] += 1
            # 讲稿正在讲的那一步说明问题在问什么；不在讲稿里时按学生的原话对应概念。
            where = data.get("script") if isinstance(data.get("script"), Mapping) else None
            context = self.steps.get((session, str(where.get("id")), _int(where.get("step")))) if where else None
            for concept in context or self.concepts.mentioned(str(data.get("text", ""))):
                self.state(concept).questions += 1
        elif kind == "ai.answer":
            self.answers[str(data.get("verdict", "unjudged"))] += 1
            self._judged(found, data, at, course, "answer")
        elif kind == "ai.practice" and data.get("stage") == "assigned":
            self._remember(self.open_practice, str(data.get("task", "")), {"task": data.get("task"), "course": course, "concepts": found}, OPEN_TRACKED)
        elif kind == "ai.practice" and data.get("stage") == "graded":
            self._judged(found, data, at, course, "practice")
            if data.get("verdict") == "pass":
                self.open_practice.pop(str(data.get("task", "")), None)
                self.counts["passedPractice"] += 1
                if isinstance(data.get("attempt"), int):
                    self.attempts = [self.attempts[0] + data["attempt"], self.attempts[1] + 1]
            if isinstance(data.get("asked"), int):
                self.asked = [self.asked[0] + data["asked"], self.asked[1] + 1]
        elif kind == "run.finished" and data.get("status") == "error":
            error = data.get("error")
            if isinstance(error, Mapping) and isinstance(error.get("type"), str):
                self.errors[error["type"]] += 1

    def _judged(self, found: list[str], data: Mapping[str, object], at: str | None, course: str, kind: str) -> None:
        verdict = data.get("verdict")
        if verdict not in ("pass", "partial", "fail"):
            return
        for concept in found:
            item = self.state(concept)
            (item.practice if kind == "practice" else item.answers)[str(verdict)] += 1
            item.outcome(str(verdict), at, True)
        misconception = data.get("misconception")
        if isinstance(misconception, str) and misconception.strip() and verdict != "pass":
            entry = self.misconceptions.setdefault(misconception, {"id": misconception, "concepts": found, "count": 0, "course": course, "last": at})
            entry["count"] = int(entry["count"]) + 1
            if at is not None and (entry["last"] is None or at >= str(entry["last"])):
                entry.update(course=course, last=at)
            for concept in found:
                self.state(concept).misconceptions[misconception] += 1

    @staticmethod
    def _remember(table: OrderedDict, key: object, value: object, limit: int) -> None:
        table[key] = value
        table.move_to_end(key)
        while len(table) > limit:
            table.popitem(last=False)

    def profile(self) -> dict[str, object]:
        rows = [_row(concept, item, self.concepts) for concept, item in self.states.items()]
        rows.sort(key=lambda row: (("shaky", "learning", "solid").index(str(row["status"])), str(row["concept"])))
        kept = sorted(self.misconceptions.values(), key=lambda entry: (-int(entry["count"]), str(entry["last"] or "")))[:MISCONCEPTIONS_KEPT]
        return {
            "version": 1,
            "knowledge": self.concepts.version,
            "lastActive": self.last_active,
            "concepts": rows,
            "misconceptions": [dict(entry) for entry in kept],
            "habits": {
                "questions": self.counts["questions"],
                "explains": self.counts["explains"],
                "answers": dict(self.answers),
                "predictions": dict(self.predictions),
                "passedPractice": self.counts["passedPractice"],
                "attemptsToPass": _mean(self.attempts),
                "askedBeforeSubmit": _mean(self.asked),
                "errors": dict(self.errors.most_common(5)),
            },
            "open": list(self.open_practice.values())[-OPEN_KEPT:],
        }

    def recent(self, limit: int) -> list[dict[str, object]]:
        """学生主动的交互，最近的在前。"""
        return list(reversed(self.activity))[:limit]


def profile_of(evidence: Iterable[Mapping[str, object]], knowledge: Mapping[str, object] | None) -> dict[str, object]:
    """evidence 是 learning.evidence.recorded 记录；同一台设备的按日志顺序。"""
    learner = Learner(knowledge)
    for record in evidence:
        learner.feed(record)
    return learner.profile()


def _row(concept: str, item: _Concept, concepts: Concepts) -> dict[str, object]:
    last_two = item.outcomes[-2:]
    last = last_two[-1][1] if last_two else None
    if last in ("partial", "fail") \
            or (item.predicted >= 2 and item.predicted_right / item.predicted < 0.5) \
            or any(count >= 2 for count in item.misconceptions.values()):
        status = "shaky"
    # 掌握：最近两次都通过，其中至少一次是练习或回答；预测题是选择题，单靠它不算。
    elif [verdict for _, verdict, _ in last_two] == ["pass", "pass"] and any(judged for _, _, judged in last_two):
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
    if item.predicted:
        row["predictions"] = {"correct": item.predicted_right, "total": item.predicted}
    if item.misconceptions:
        row["misconceptions"] = [name for name, _ in item.misconceptions.most_common(3)]
    times = [at for at, _, _ in item.outcomes if at]
    if times:
        row["lastPracticed"] = times[-1]
    if status == "solid" and item.last_pass is not None:
        days = REVIEW_DAYS[min(item.passes - 2, len(REVIEW_DAYS) - 1)]
        row["reviewDue"] = (_parse(item.last_pass) + timedelta(days=days)).isoformat()
    return row


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


def _mean(total_and_count: list[int]) -> float | None:
    total, count = total_and_count
    return round(total / count, 1) if count else None


def _int(value: object) -> int:
    return value if isinstance(value, int) else -1


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
