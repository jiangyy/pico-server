from __future__ import annotations

import math
import hashlib
import struct
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

_HASH_PREFIX = "sha256:"


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


def _size(value: int) -> bytes:
    if not 0 <= value <= 0xFFFF_FFFF:
        raise InvalidReplicaData("value is too large")
    return struct.pack(">I", value)


def _raw(value: bytes) -> bytes:
    return _size(len(value)) + value


def _text(value: str) -> bytes:
    return _raw(value.encode("utf-8"))


def _value_bytes(value: JsonValue) -> bytes:
    if value is None:
        return b"\x00"
    if value is False:
        return b"\x01"
    if value is True:
        return b"\x02"
    if type(value) in (int, float):
        number = float(value)
        if not math.isfinite(number):
            raise InvalidReplicaData("data must contain only finite numbers")
        return b"\x03" + _raw(struct.pack(">d", number))
    if isinstance(value, str):
        return b"\x04" + _text(value)
    if isinstance(value, tuple):
        return b"\x05" + _size(len(value)) + b"".join(
            _value_bytes(item) for item in value
        )
    entries = sorted(
        ((key.encode("utf-8"), item) for key, item in value.items()),
        key=lambda entry: entry[0],
    )
    return b"\x06" + _size(len(entries)) + b"".join(
        _raw(key) + _value_bytes(item) for key, item in entries
    )


def _position_bytes(position: "Position | None") -> bytes:
    if position is None:
        return b"\x00"
    return b"\x01" + _text(position.log_id) + _text(position.record_id)


def valid_object_id(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith(_HASH_PREFIX):
        return False
    digest = value[len(_HASH_PREFIX) :]
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


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
    objects: tuple[str, ...]
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
        if type(self.objects) is not tuple or any(
            not valid_object_id(item) for item in self.objects
        ):
            raise InvalidReplicaData("record.objects must contain ObjectIds")
        object.__setattr__(self, "data", _freeze_json(self.data))
        if self.id != record_id_of(
            self.log_id, self.parent, self.requires, self.objects, self.data
        ):
            raise InvalidReplicaData("record identity does not match its content")

    @property
    def position(self) -> Position:
        return Position(self.log_id, self.id)

    @classmethod
    def from_value(
        cls, value: object, *, log_id: str | None = None
    ) -> "ReplicatedRecord":
        fields = {"id", "logId", "parent", "requires", "objects", "data"}
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
        objects_value = value["objects"]
        if not isinstance(objects_value, list):
            raise InvalidReplicaData("record.objects must be an array")
        return cls(
            id=_non_empty(value["id"], "record.id"),
            log_id=actual_log_id,
            parent=parent,
            requires=tuple(Position.from_value(item) for item in requires_value),
            objects=tuple(objects_value),
            data=value["data"],
        )

    def to_value(self) -> dict[str, object]:
        return {
            "id": self.id,
            "logId": self.log_id,
            "parent": None if self.parent is None else self.parent.to_value(),
            "requires": [item.to_value() for item in self.requires],
            "objects": list(self.objects),
            "data": plain_json(self.data),
        }


def record_id_of(
    log_id: str,
    parent: Position | None,
    requires: tuple[Position, ...],
    objects: tuple[str, ...],
    data: JsonValue,
) -> str:
    payload = b"".join(
        (
            _text("pico.replica.record.v1"),
            _text(log_id),
            _position_bytes(parent),
            _size(len(requires)),
            *(_position_bytes(item) for item in requires),
            _size(len(objects)),
            *(_text(item) for item in objects),
            _value_bytes(data),
        )
    )
    return _HASH_PREFIX + hashlib.sha256(payload).hexdigest()


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
