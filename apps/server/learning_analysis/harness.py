"""Orchestrates event storage, profile projection and one Agent call."""

from __future__ import annotations

from typing import Protocol

from apps.server.learning_analysis.agent import (
    AgentContext,
    AgentOutput,
    LearningActivity,
    LearningAgent,
)
from apps.server.learning_analysis.core import (
    LearnerProfile,
    LearningEvent,
    SkillRule,
    project_profile,
)


class EventStore(Protocol):
    def save(self, events: tuple[LearningEvent, ...]) -> None: ...

    def for_student(self, student_id: str) -> tuple[LearningEvent, ...]: ...


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: dict[str, LearningEvent] = {}

    def save(self, events: tuple[LearningEvent, ...]) -> None:
        for event in events:
            existing = self._events.get(event.event_id)
            if existing is not None and existing != event:
                raise ValueError(f"event id conflict: {event.event_id}")
            self._events[event.event_id] = event

    def for_student(self, student_id: str) -> tuple[LearningEvent, ...]:
        return tuple(event for event in self._events.values() if event.student_id == student_id)


class LearningAnalysisHarness:
    def __init__(
        self,
        store: EventStore,
        rules: tuple[SkillRule, ...],
        catalog: tuple[LearningActivity, ...],
        agent: LearningAgent,
    ) -> None:
        self._store = store
        self._rules = rules
        self._catalog = catalog
        self._agent = agent

    def record(self, events: tuple[LearningEvent, ...]) -> LearnerProfile:
        if not events:
            raise ValueError("events must not be empty")
        student_id = events[0].student_id
        if any(event.student_id != student_id for event in events):
            raise ValueError("one record call accepts events from one student")
        self._store.save(events)
        return self.profile(student_id)

    def profile(self, student_id: str) -> LearnerProfile:
        events = self._store.for_student(student_id)
        return project_profile(student_id, events, self._rules)

    def analyze(
        self,
        student_id: str,
        attempt_id: str,
        learning_goal: str | None = None,
    ) -> AgentOutput:
        events = self._store.for_student(student_id)
        current = tuple(event for event in events if event.attempt_id == attempt_id)
        if not current:
            raise ValueError(f"attempt not found: {attempt_id}")
        task_refs = {(event.task_id, event.task_version) for event in current}
        if len(task_refs) != 1:
            raise ValueError("one attempt must reference one activity version")
        task_id, task_version = next(iter(task_refs))
        activity = next(
            (
                item for item in self._catalog
                if item.activity_id == task_id and item.version == task_version
            ),
            None,
        )
        if activity is None:
            raise ValueError(f"activity not found: {task_id}@{task_version}")
        context = AgentContext(
            profile=project_profile(student_id, events, self._rules),
            current_activity=activity,
            current_attempt_id=attempt_id,
            events=events,
            learning_goal=learning_goal,
            catalog=self._catalog,
        )
        output = self._agent.analyze(context)
        self._validate_output(context, output)
        return output

    @staticmethod
    def _validate_output(context: AgentContext, output: AgentOutput) -> None:
        known_events = {event.event_id for event in context.events}
        cited = {
            evidence_id
            for finding in (*output.strengths, *output.needs, output.guidance)
            for evidence_id in finding.evidence_ids
        }
        unknown = cited - known_events
        if unknown:
            raise ValueError(f"agent cited unknown events: {sorted(unknown)}")
        if output.plan is not None and output.plan.activity_id not in {
            item.activity_id for item in context.catalog
        }:
            raise ValueError(f"agent selected unknown activity: {output.plan.activity_id}")
