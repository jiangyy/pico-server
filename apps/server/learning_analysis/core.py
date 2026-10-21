"""Learning facts and the pure learner-profile projection."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Literal

EventType = Literal[
    "task.opened",
    "code.saved",
    "run.finished",
    "test.finished",
    "response.saved",
    "help.shown",
    "activity.finished",
]
Result = Literal["success", "partial", "failure"]
Assistance = Literal["none", "hint", "solution"]


@dataclass(frozen=True)
class LearningEvent:
    event_id: str
    student_id: str
    attempt_id: str
    task_id: str
    task_version: int
    sequence: int
    type: EventType
    source: str | None = None
    run_output: str | None = None
    response: str | None = None
    skill_id: str | None = None
    case_id: str | None = None
    test_result: Literal["pass", "fail"] | None = None
    help_level: Literal["hint", "solution"] | None = None

    def __post_init__(self) -> None:
        required = {
            "code.saved": self.source,
            "run.finished": self.run_output,
            "response.saved": self.response,
            "help.shown": self.help_level,
        }
        if self.type in required and required[self.type] is None:
            raise ValueError(f"{self.type} is missing its value")
        if self.type == "test.finished" and not (
            self.skill_id and self.case_id and self.test_result
        ):
            raise ValueError("test.finished requires skill_id, case_id and test_result")


@dataclass(frozen=True)
class SkillRule:
    task_id: str
    task_version: int
    skill_id: str
    case_ids: tuple[str, ...]


@dataclass(frozen=True)
class SkillObservation:
    attempt_id: str
    task_id: str
    task_version: int
    skill_id: str
    result: Result
    assistance: Assistance
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class OutcomeCounts:
    success: int
    partial: int
    failure: int


@dataclass(frozen=True)
class AssistanceOutcomes:
    assistance: Assistance
    outcomes: OutcomeCounts


@dataclass(frozen=True)
class SkillProfile:
    skill_id: str
    attempts: int
    outcomes: OutcomeCounts
    by_assistance: tuple[AssistanceOutcomes, ...]
    observations: tuple[SkillObservation, ...]


@dataclass(frozen=True)
class BehaviorCount:
    event_type: EventType
    count: int


@dataclass(frozen=True)
class LearnerProfile:
    student_id: str
    event_count: int
    skills: tuple[SkillProfile, ...]
    behaviors: tuple[BehaviorCount, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def project_profile(
    student_id: str,
    events: tuple[LearningEvent, ...],
    rules: tuple[SkillRule, ...],
) -> LearnerProfile:
    """Derive the current profile from immutable learning events."""
    student_events = _unique_events(student_id, events)
    observations = _observe(student_events, rules)
    by_skill: dict[str, list[SkillObservation]] = defaultdict(list)
    for observation in observations:
        by_skill[observation.skill_id].append(observation)

    skills = tuple(
        _skill_profile(skill_id, tuple(items))
        for skill_id, items in sorted(by_skill.items())
    )
    behavior_types: tuple[EventType, ...] = ("code.saved", "run.finished", "help.shown")
    behavior_counts = Counter(event.type for event in student_events)
    behaviors = tuple(
        BehaviorCount(event_type, behavior_counts[event_type])
        for event_type in behavior_types
        if behavior_counts[event_type]
    )
    return LearnerProfile(student_id, len(student_events), skills, behaviors)


def _unique_events(
    student_id: str, events: tuple[LearningEvent, ...]
) -> tuple[LearningEvent, ...]:
    unique: dict[str, LearningEvent] = {}
    for event in events:
        if event.student_id != student_id:
            continue
        existing = unique.get(event.event_id)
        if existing is not None and existing != event:
            raise ValueError(f"event id conflict: {event.event_id}")
        unique[event.event_id] = event
    return tuple(unique.values())


def _observe(
    events: tuple[LearningEvent, ...], rules: tuple[SkillRule, ...]
) -> tuple[SkillObservation, ...]:
    rule_by_key = {
        (rule.task_id, rule.task_version, rule.skill_id): rule for rule in rules
    }
    attempts: dict[tuple[str, str, int], list[LearningEvent]] = defaultdict(list)
    for event in events:
        attempts[event.attempt_id, event.task_id, event.task_version].append(event)

    observations: list[SkillObservation] = []
    for (attempt_id, task_id, task_version), attempt in sorted(attempts.items()):
        ordered = sorted(attempt, key=lambda event: event.sequence)
        tests: dict[tuple[str, str], LearningEvent] = {}
        for event in ordered:
            if event.type == "test.finished":
                assert event.skill_id is not None and event.case_id is not None
                tests[event.skill_id, event.case_id] = event

        for skill_id in sorted({skill_id for skill_id, _ in tests}):
            rule = rule_by_key.get((task_id, task_version, skill_id))
            if rule is None:
                continue
            judged = tuple(
                tests[skill_id, case_id]
                for case_id in rule.case_ids
                if (skill_id, case_id) in tests
            )
            if not judged:
                continue
            passed = sum(event.test_result == "pass" for event in judged)
            result: Result = (
                "success"
                if passed == len(rule.case_ids)
                else "partial"
                if passed
                else "failure"
            )
            last_test = max(event.sequence for event in judged)
            assistance: Assistance = "none"
            for event in ordered:
                if event.type != "help.shown" or event.sequence >= last_test:
                    continue
                if event.help_level == "solution":
                    assistance = "solution"
                elif assistance == "none":
                    assistance = "hint"
            observations.append(
                SkillObservation(
                    attempt_id,
                    task_id,
                    task_version,
                    skill_id,
                    result,
                    assistance,
                    tuple(event.event_id for event in judged),
                )
            )
    return tuple(observations)


def _skill_profile(
    skill_id: str, observations: tuple[SkillObservation, ...]
) -> SkillProfile:
    outcomes = _outcomes(observations)
    assistance_levels: tuple[Assistance, ...] = ("none", "hint", "solution")
    by_assistance = tuple(
        AssistanceOutcomes(
            assistance,
            _outcomes(tuple(item for item in observations if item.assistance == assistance)),
        )
        for assistance in assistance_levels
        if any(item.assistance == assistance for item in observations)
    )
    return SkillProfile(skill_id, len(observations), outcomes, by_assistance, observations)


def _outcomes(observations: tuple[SkillObservation, ...]) -> OutcomeCounts:
    counts = Counter(item.result for item in observations)
    return OutcomeCounts(counts["success"], counts["partial"], counts["failure"])
