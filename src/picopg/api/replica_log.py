from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, TypeAlias


JsonValue: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | tuple["JsonValue", ...]
    | Mapping[str, "JsonValue"]
)


class ReplicaLogError(Exception):
    pass


class InvalidReplicaData(ReplicaLogError):
    pass


class UnknownPosition(ReplicaLogError):
    pass


class MissingDependency(ReplicaLogError):
    pass


def _non_empty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidReplicaData(f"{label} must be a non-empty string")
    return value


def _freeze_json(value: object, seen: set[int] | None = None) -> JsonValue:
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise InvalidReplicaData("data must contain only finite JSON values")
        return value
    if seen is None:
        seen = set()
    if not isinstance(value, (Mapping, list, tuple)) or id(value) in seen:
        raise InvalidReplicaData("data must be a finite JSON value")
    seen.add(id(value))
    try:
        if isinstance(value, Mapping):
            if any(not isinstance(key, str) for key in value):
                raise InvalidReplicaData("JSON object keys must be strings")
            return MappingProxyType(
                {key: _freeze_json(item, seen) for key, item in value.items()}
            )
        return tuple(_freeze_json(item, seen) for item in value)
    finally:
        seen.remove(id(value))


def plain_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Position:
    log_id: str
    record_id: str

    def __post_init__(self) -> None:
        _non_empty(self.log_id, "logId")
        _non_empty(self.record_id, "recordId")

    @classmethod
    def from_value(
        cls, value: object, *, log_id: str | None = None
    ) -> "Position":
        if not isinstance(value, dict) or set(value) != {"logId", "recordId"}:
            raise InvalidReplicaData("Position must contain logId and recordId")
        result = cls(
            log_id=_non_empty(value["logId"], "logId"),
            record_id=_non_empty(value["recordId"], "recordId"),
        )
        if log_id is not None and result.log_id != log_id:
            raise InvalidReplicaData("Position belongs to another Log")
        return result

    def to_value(self) -> dict[str, str]:
        return {"logId": self.log_id, "recordId": self.record_id}


@dataclass(frozen=True, slots=True)
class ReplicatedRecord:
    id: str
    log_id: str
    parent: Position | None
    requires: tuple[Position, ...]
    data: JsonValue

    def __post_init__(self) -> None:
        _non_empty(self.id, "record.id")
        _non_empty(self.log_id, "record.logId")
        if self.parent is not None:
            if not isinstance(self.parent, Position):
                raise InvalidReplicaData("record.parent must be a Position or null")
            if self.parent.log_id != self.log_id:
                raise InvalidReplicaData("record.parent belongs to another Log")
        if type(self.requires) is not tuple or any(
            not isinstance(item, Position) for item in self.requires
        ):
            raise InvalidReplicaData("record.requires must be Positions")
        if any(item.log_id == self.log_id for item in self.requires):
            raise InvalidReplicaData("same-Log causality must use parent")
        object.__setattr__(self, "data", _freeze_json(self.data))

    @property
    def position(self) -> Position:
        return Position(self.log_id, self.id)

    @classmethod
    def from_value(
        cls, value: object, *, log_id: str | None = None
    ) -> "ReplicatedRecord":
        fields = {"id", "logId", "parent", "requires", "data"}
        if not isinstance(value, dict) or set(value) != fields:
            raise InvalidReplicaData("record shape is invalid")
        actual_log_id = _non_empty(value["logId"], "record.logId")
        if log_id is not None and actual_log_id != log_id:
            raise InvalidReplicaData("record belongs to another Log")
        parent_value = value["parent"]
        parent = (
            None
            if parent_value is None
            else Position.from_value(parent_value, log_id=actual_log_id)
        )
        requires_value = value["requires"]
        if not isinstance(requires_value, list):
            raise InvalidReplicaData("record.requires must be an array")
        return cls(
            id=_non_empty(value["id"], "record.id"),
            log_id=actual_log_id,
            parent=parent,
            requires=tuple(Position.from_value(item) for item in requires_value),
            data=value["data"],
        )

    def to_value(self) -> dict[str, object]:
        return {
            "id": self.id,
            "logId": self.log_id,
            "parent": None if self.parent is None else self.parent.to_value(),
            "requires": [item.to_value() for item in self.requires],
            "data": plain_json(self.data),
        }


@dataclass(frozen=True, slots=True)
class LogPage:
    records: tuple[ReplicatedRecord, ...]
    head: Position | None
    has_more: bool

    def to_value(self) -> dict[str, object]:
        return {
            "records": [record.to_value() for record in self.records],
            "head": None if self.head is None else self.head.to_value(),
            "hasMore": self.has_more,
        }


@dataclass(frozen=True, slots=True)
class AppendResult:
    kind: str
    head: Position | None

    def __post_init__(self) -> None:
        if self.kind not in ("accepted", "current", "changed"):
            raise ValueError("invalid append result kind")

    def to_value(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "head": None if self.head is None else self.head.to_value(),
        }


class ReplicaLogStore(Protocol):
    def head(self, log_id: str) -> Position | None: ...

    def has(self, position: Position) -> bool: ...

    def read(
        self, log_id: str, after: Position | None, limit: int
    ) -> LogPage: ...

    def append(
        self,
        log_id: str,
        expected_head: Position | None,
        records: tuple[ReplicatedRecord, ...],
    ) -> AppendResult: ...
