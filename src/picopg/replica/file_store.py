from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from uuid import uuid4

from picopg.replica.strict_json import strict_json_loads
from picopg.replica import (
    AppendResult,
    InvalidReplicaData,
    LogPage,
    MissingDependency,
    Position,
    ReplicaLogStore,
    ReplicatedRecord,
    UnknownPosition,
)


class ReplicaLogFileError(Exception):
    pass


class FileReplicaLogStore(ReplicaLogStore):
    """One-process durable Log store.

    shortcut: one process owns the directory; add a file lock before allowing
    multiple server processes to open the same store.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._path = root / "replica-log.json"
        self._lock = RLock()
        self._logs = self._load()

    def log_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._logs))

    def head(self, log_id: str) -> Position | None:
        _log_id(log_id)
        with self._lock:
            return _head(log_id, self._logs.get(log_id, ()))

    def has(self, position: Position) -> bool:
        if not isinstance(position, Position):
            raise InvalidReplicaData("position must be a Position")
        with self._lock:
            return any(
                record.id == position.record_id
                for record in self._logs.get(position.log_id, ())
            )

    def read(
        self, log_id: str, after: Position | None, limit: int
    ) -> LogPage:
        _log_id(log_id)
        if type(limit) is not int or limit < 1:
            raise InvalidReplicaData("limit must be a positive integer")
        if after is not None and (
            not isinstance(after, Position) or after.log_id != log_id
        ):
            raise InvalidReplicaData("after belongs to another Log")
        with self._lock:
            records = self._logs.get(log_id, ())
            start = 0
            if after is not None:
                for index, record in enumerate(records):
                    if record.id == after.record_id:
                        start = index + 1
                        break
                else:
                    raise UnknownPosition(
                        f"unknown cursor {log_id}:{after.record_id}"
                    )
            page = records[start : start + limit]
            return LogPage(
                records=tuple(
                    ReplicatedRecord.from_value(record.to_value())
                    for record in page
                ),
                head=_head(log_id, records),
                has_more=start + len(page) < len(records),
            )

    def append(
        self,
        log_id: str,
        expected_head: Position | None,
        records: tuple[ReplicatedRecord, ...],
    ) -> AppendResult:
        _log_id(log_id)
        if expected_head is not None and (
            not isinstance(expected_head, Position)
            or expected_head.log_id != log_id
        ):
            raise InvalidReplicaData("expectedHead belongs to another Log")
        if type(records) is not tuple or any(
            not isinstance(record, ReplicatedRecord) for record in records
        ):
            raise InvalidReplicaData("records must be ReplicatedRecords")
        batch = tuple(
            ReplicatedRecord.from_value(record.to_value(), log_id=log_id)
            for record in records
        )
        _validate_chain(log_id, expected_head, batch)

        with self._lock:
            accepted = self._logs.get(log_id, ())
            current = _head(log_id, accepted)
            self._validate_dependencies(batch)
            if current != expected_head:
                replayed = bool(batch) and all(
                    record in accepted for record in batch
                )
                return AppendResult(
                    "current" if replayed else "changed", current
                )
            if not batch:
                return AppendResult("current", current)

            known = {record.id for record in accepted}
            for record in batch:
                if record.id in known:
                    raise InvalidReplicaData(
                        f"record id reused: {record.id}"
                    )
                known.add(record.id)

            next_logs = dict(self._logs)
            next_logs[log_id] = (*accepted, *batch)
            self._write(next_logs)
            self._logs = next_logs
            return AppendResult("accepted", batch[-1].position)

    def _validate_dependencies(
        self, records: tuple[ReplicatedRecord, ...]
    ) -> None:
        for record in records:
            for required in record.requires:
                if not any(
                    candidate.id == required.record_id
                    for candidate in self._logs.get(required.log_id, ())
                ):
                    raise MissingDependency(
                        f"missing dependency "
                        f"{required.log_id}:{required.record_id}"
                    )

    def _load(self) -> dict[str, tuple[ReplicatedRecord, ...]]:
        try:
            value = strict_json_loads(self._path.read_text(encoding="utf-8"))
            logs = _decode_document(value)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, InvalidReplicaData) as error:
            raise ReplicaLogFileError(
                f"cannot open {self._path}"
            ) from error
        _validate_document_dependencies(logs)
        return logs

    def _write(
        self, logs: dict[str, tuple[ReplicatedRecord, ...]]
    ) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = self._root / f".replica-log-{uuid4().hex}.tmp"
        document = {
            "schema": 1,
            "logs": [
                {
                    "logId": log_id,
                    "records": [record.to_value() for record in records],
                }
                for log_id, records in logs.items()
            ],
        }
        payload = (
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
            try:
                directory = os.open(self._root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError:
                pass
        except OSError as error:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise ReplicaLogFileError(
                f"cannot persist {self._path}"
            ) from error


def _log_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidReplicaData("logId must be a non-empty string")
    return value


def _head(
    log_id: str, records: tuple[ReplicatedRecord, ...]
) -> Position | None:
    return None if not records else Position(log_id, records[-1].id)


def _validate_chain(
    log_id: str,
    expected_head: Position | None,
    records: tuple[ReplicatedRecord, ...],
) -> None:
    parent = expected_head
    ids: set[str] = set()
    for record in records:
        if record.log_id != log_id or record.parent != parent:
            raise InvalidReplicaData(
                f"broken parent before {record.id}"
            )
        if record.id in ids:
            raise InvalidReplicaData(f"duplicate record {record.id}")
        ids.add(record.id)
        parent = record.position


def _decode_document(
    value: object,
) -> dict[str, tuple[ReplicatedRecord, ...]]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "logs"}
        or value["schema"] != 1
        or not isinstance(value["logs"], list)
    ):
        raise InvalidReplicaData("document schema is invalid")
    logs: dict[str, tuple[ReplicatedRecord, ...]] = {}
    for entry in value["logs"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"logId", "records"}
            or not isinstance(entry["records"], list)
        ):
            raise InvalidReplicaData("log entry is invalid")
        log_id = _log_id(entry["logId"])
        if log_id in logs:
            raise InvalidReplicaData(f"duplicate Log {log_id}")
        records = tuple(
            ReplicatedRecord.from_value(record, log_id=log_id)
            for record in entry["records"]
        )
        _validate_chain(log_id, None, records)
        logs[log_id] = records
    return logs


def _validate_document_dependencies(
    logs: dict[str, tuple[ReplicatedRecord, ...]]
) -> None:
    positions = {
        record.position
        for records in logs.values()
        for record in records
    }
    for records in logs.values():
        for record in records:
            for required in record.requires:
                if required not in positions:
                    raise ReplicaLogFileError(
                        f"missing dependency "
                        f"{required.log_id}:{required.record_id}"
                    )
