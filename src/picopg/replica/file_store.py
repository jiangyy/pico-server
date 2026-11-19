from __future__ import annotations

import hashlib
import json
import os
import shutil
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


LEGACY_DOCUMENT = "replica-log.json"
LOG_DIRECTORY = "logs"


class FileReplicaLogStore(ReplicaLogStore):
    """One-process durable Log store: one append-only JSON Lines file per Log.

    Each file starts with a header line naming its Log, then one Record per line. An append
    writes and fsyncs only that Log's new lines, so its cost does not grow with the store.
    Records stay in memory with an index from Record ID to position; they are immutable, so
    reads hand them out without copying.

    A crash can leave an unterminated last line: that append was never acknowledged, so
    opening the store cuts it off. A store written by the earlier single-document format is
    migrated once on open and the old document is kept beside it.

    shortcut: one process owns the directory; add a file lock before allowing
    multiple server processes to open the same store.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._directory = root / LOG_DIRECTORY
        self._lock = RLock()
        # 只在锁里读写；记录不可变，读的时候切一段交出去。
        self._logs: dict[str, list[ReplicatedRecord]] = {}
        self._index: dict[str, dict[str, int]] = {}
        # 磁盘上已经有文件（至少有完整 header）的 Log：只有它们追加时不写 header。
        self._files: set[str] = set()
        self._migrate_legacy()
        self._load()

    def log_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._logs))

    def head(self, log_id: str) -> Position | None:
        _log_id(log_id)
        with self._lock:
            return _head(log_id, self._logs.get(log_id, []))

    def has(self, position: Position) -> bool:
        if not isinstance(position, Position):
            raise InvalidReplicaData("position must be a Position")
        with self._lock:
            return position.record_id in self._index.get(position.log_id, {})

    def record(self, position: Position) -> ReplicatedRecord | None:
        if not isinstance(position, Position):
            raise InvalidReplicaData("position must be a Position")
        with self._lock:
            index = self._index.get(position.log_id, {}).get(position.record_id)
            return None if index is None else self._logs[position.log_id][index]

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
            records = self._logs.get(log_id, [])
            start = 0
            if after is not None:
                index = self._index.get(log_id, {}).get(after.record_id)
                if index is None:
                    raise UnknownPosition(
                        f"unknown cursor {log_id}:{after.record_id}"
                    )
                start = index + 1
            page = tuple(records[start : start + limit])
            return LogPage(
                records=page,
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
            accepted = self._logs.get(log_id, [])
            index = self._index.get(log_id, {})
            current = _head(log_id, accepted)
            self._validate_dependencies(batch)
            if current != expected_head:
                replayed = bool(batch) and all(
                    record.id in index and accepted[index[record.id]] == record
                    for record in batch
                )
                return AppendResult(
                    "current" if replayed else "changed", current
                )
            if not batch:
                return AppendResult("current", current)
            for record in batch:
                if record.id in index:
                    raise InvalidReplicaData(
                        f"record id reused: {record.id}"
                    )

            self._write(log_id, batch, created=log_id not in self._files)
            self._files.add(log_id)
            records = self._logs.setdefault(log_id, [])
            positions = self._index.setdefault(log_id, {})
            for record in batch:
                positions[record.id] = len(records)
                records.append(record)
            return AppendResult("accepted", batch[-1].position)

    def _validate_dependencies(
        self, records: tuple[ReplicatedRecord, ...]
    ) -> None:
        for record in records:
            for required in record.requires:
                if required.record_id not in self._index.get(required.log_id, {}):
                    raise MissingDependency(
                        f"missing dependency "
                        f"{required.log_id}:{required.record_id}"
                    )

    def _write(
        self, log_id: str, batch: tuple[ReplicatedRecord, ...], *, created: bool
    ) -> None:
        path = self._directory / _file_name(log_id)
        lines = [_line(record.to_value()) for record in batch]
        if created:
            lines.insert(0, _line({"logId": log_id}))
        payload = b"".join(lines)
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                written = 0
                while written < len(payload):
                    written += os.write(descriptor, payload[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if created:
                _fsync_directory(self._directory)
        except OSError as error:
            raise ReplicaLogFileError(f"cannot persist {path}") from error

    def _load(self) -> None:
        if not self._directory.exists():
            return
        for path in sorted(self._directory.glob("*.jsonl")):
            try:
                loaded = _read_log(path)
            except (OSError, ValueError, TypeError, InvalidReplicaData) as error:
                raise ReplicaLogFileError(f"cannot open {path}") from error
            if loaded is None:
                # 建 Log 的那次写入连 header 都没写完：从没被确认过，当作不存在。
                path.unlink()
                continue
            log_id, records = loaded
            if path.name != _file_name(log_id):
                raise ReplicaLogFileError(f"{path} holds another Log ({log_id})")
            self._files.add(log_id)
            self._logs[log_id] = list(records)
            self._index[log_id] = {record.id: index for index, record in enumerate(records)}
        _validate_document_dependencies(self._logs)

    def _migrate_legacy(self) -> None:
        """旧格式（整个库一份 JSON）只在第一次打开时搬成每个 Log 一个文件；旧文件改名留着，确认无误后再删。"""
        legacy = self._root / LEGACY_DOCUMENT
        if not legacy.exists() or self._directory.exists():
            return
        try:
            logs = _decode_document(strict_json_loads(legacy.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, InvalidReplicaData) as error:
            raise ReplicaLogFileError(f"cannot open {legacy}") from error
        _validate_document_dependencies(logs)
        staging = self._root / f".{LOG_DIRECTORY}-{uuid4().hex}.tmp"
        try:
            staging.mkdir(parents=True)
            for log_id, records in logs.items():
                payload = b"".join([_line({"logId": log_id}), *(_line(record.to_value()) for record in records)])
                descriptor = os.open(staging / _file_name(log_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    written = 0
                    while written < len(payload):
                        written += os.write(descriptor, payload[written:])
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            _fsync_directory(staging)
            os.replace(staging, self._directory)
            os.replace(legacy, self._root / f"{LEGACY_DOCUMENT}.migrated")
            _fsync_directory(self._root)
        except OSError as error:
            shutil.rmtree(staging, ignore_errors=True)
            raise ReplicaLogFileError(f"cannot migrate {legacy}") from error


def _file_name(log_id: str) -> str:
    # Log ID 里有 `/` 和编码过的字符：文件名取它的摘要，Log ID 本身写在文件第一行。
    return hashlib.sha256(log_id.encode("utf-8")).hexdigest() + ".jsonl"


def _line(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _read_log(path: Path) -> tuple[str, tuple[ReplicatedRecord, ...]] | None:
    """读一个 Log 文件；连完整的 header 都没有时为 None。"""
    data = path.read_bytes()
    complete = data.rfind(b"\n") + 1
    if complete < len(data):
        # 崩溃时没写完的最后一行：那次追加没有被确认过，截掉。
        with open(path, "r+b") as output:
            output.truncate(complete)
            output.flush()
            os.fsync(output.fileno())
        data = data[:complete]
    lines = data.decode("utf-8").splitlines()
    if not lines:
        return None
    header = strict_json_loads(lines[0])
    if not isinstance(header, dict) or set(header) != {"logId"}:
        raise InvalidReplicaData("Log file header is invalid")
    log_id = _log_id(header["logId"])
    records = tuple(
        ReplicatedRecord.from_value(strict_json_loads(line), log_id=log_id)
        for line in lines[1:]
    )
    _validate_chain(log_id, None, records)
    return log_id, records


def _fsync_directory(path: Path) -> None:
    try:
        directory = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        pass


def _log_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidReplicaData("logId must be a non-empty string")
    return value


def _head(
    log_id: str, records: "tuple[ReplicatedRecord, ...] | list[ReplicatedRecord]"
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
    logs: "dict[str, tuple[ReplicatedRecord, ...]] | dict[str, list[ReplicatedRecord]]"
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
