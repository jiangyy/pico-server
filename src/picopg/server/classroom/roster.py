"""Server-owned class rosters and their teaching projection."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from picopg.server.common.reads import PaginationStalled, read_all
from picopg.server.common.validation import require_non_blank_text
from picopg.replica import Position, ReplicaLogStore, ReplicatedRecord, record_id_of


ROSTER_KIND = "class.roster"
_CLASS_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_LOG_PREFIX = "class/"
_LOG_SUFFIX = "/roster"


class ClassRosterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ClassRoster:
    teachers: frozenset[str]
    students: frozenset[str]


@dataclass(frozen=True, slots=True)
class TeachingDirectory:
    rosters: tuple[ClassRoster, ...]

    def students_of(self, teacher: str) -> frozenset[str]:
        return frozenset(
            student
            for roster in self.rosters
            if teacher in roster.teachers
            for student in roster.students
        )

class ClassRosterServer(Protocol):
    @property
    def store(self) -> ReplicaLogStore: ...

    def append_and_broadcast(
        self, log_id: str, expected_head: Position | None,
        records: tuple[ReplicatedRecord, ...],
    ) -> object: ...


def class_roster_log_id(class_id: str) -> str:
    if not isinstance(class_id, str) or _CLASS_ID.fullmatch(class_id) is None:
        raise ClassRosterError("classId is not a valid identifier")
    return f"{_LOG_PREFIX}{class_id}{_LOG_SUFFIX}"


class ClassRosterRepository:
    def __init__(
        self,
        server: ClassRosterServer,
        known_principals: Iterable[str],
    ) -> None:
        self._server = server
        self._store = server.store
        self._known_principals = frozenset(known_principals)
        self._lock = threading.Lock()

    def replace(
        self,
        class_id: str,
        teachers: object,
        students: object,
    ) -> Position:
        log_id = class_roster_log_id(class_id)
        teacher_ids = _members(teachers, "teachers", self._known_principals)
        student_ids = _members(students, "students", self._known_principals)
        if set(teacher_ids).intersection(student_ids):
            raise ClassRosterError("teachers and students must be disjoint")
        data = {
            "type": ROSTER_KIND,
            "teachers": teacher_ids,
            "students": student_ids,
        }
        with self._lock:
            parent = self._store.head(log_id)
            record = ReplicatedRecord(
                record_id_of(log_id, parent, (), data),
                log_id,
                parent,
                (),
                data,
            )
            result = self._server.append_and_broadcast(log_id, parent, (record,))
            if result.kind != "accepted":
                raise RuntimeError("class roster Log changed during a locked append")
            return record.position

    def directory(self) -> TeachingDirectory:
        rosters: list[ClassRoster] = []
        for log_id in sorted(self._store.log_ids()):
            class_id = _class_id_from_log(log_id)
            if class_id is None:
                continue
            try:
                records = read_all(self._store, log_id)
            except PaginationStalled as error:
                raise RuntimeError("class roster Log returned an empty continuation page") from error
            if records:
                rosters.append(_decode(records[-1]))
        return TeachingDirectory(tuple(rosters))


def _members(
    value: object,
    name: str,
    known_principals: frozenset[str] | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ClassRosterError(f"{name} must be an array")
    members = tuple(
        require_non_blank_text(item, name, error_type=ClassRosterError)
        for item in value
    )
    if len(set(members)) != len(members):
        raise ClassRosterError(f"{name} must not contain duplicates")
    if known_principals is not None:
        unknown = set(members) - known_principals
        if unknown:
            raise ClassRosterError(f"unknown Principal: {min(unknown)}")
    return tuple(sorted(members))


def _class_id_from_log(log_id: str) -> str | None:
    if not log_id.startswith(_LOG_PREFIX) or not log_id.endswith(_LOG_SUFFIX):
        return None
    class_id = log_id[len(_LOG_PREFIX):-len(_LOG_SUFFIX)]
    return class_id if _CLASS_ID.fullmatch(class_id) is not None else None


def _decode(record: ReplicatedRecord) -> ClassRoster:
    data = record.data
    if not isinstance(data, Mapping) or set(data) != {"type", "teachers", "students"}:
        raise ClassRosterError("class roster body is invalid")
    if data.get("type") != ROSTER_KIND:
        raise ClassRosterError("record is not a class roster")
    teachers = frozenset(_members(data.get("teachers"), "teachers"))
    students = frozenset(_members(data.get("students"), "students"))
    if teachers.intersection(students):
        raise ClassRosterError("teachers and students must be disjoint")
    return ClassRoster(teachers, students)
