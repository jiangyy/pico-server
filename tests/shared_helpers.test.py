from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from apps.server.log_reads import find_record, read_all  # noqa: E402
from apps.server.validation import (  # noqa: E402
    require_non_blank_position,
    require_non_blank_text,
    require_position,
    require_text,
)
from picopg._json import strict_json_loads  # noqa: E402
from picopg.api import (  # noqa: E402
    AppendResult,
    InvalidReplicaData,
    LogPage,
    Position,
    ReplicatedRecord,
    record_id_of,
)


class MemoryStore:
    def __init__(
        self, records: tuple[ReplicatedRecord, ...], *, stalled: bool = False
    ) -> None:
        self._records = records
        self._stalled = stalled

    def log_ids(self) -> tuple[str, ...]:
        return ("test",)

    def head(self, log_id: str) -> Position | None:
        return None if not self._records else self._records[-1].position

    def has(self, position: Position) -> bool:
        return any(record.position == position for record in self._records)

    def read(
        self, log_id: str, after: Position | None, limit: int
    ) -> LogPage:
        if self._stalled:
            return LogPage((), self.head(log_id), True)
        start = 0
        if after is not None:
            start = next(
                index + 1
                for index, record in enumerate(self._records)
                if record.position == after
            )
        page = self._records[start : start + limit]
        return LogPage(
            page,
            self.head(log_id),
            start + len(page) < len(self._records),
        )

    def append(
        self,
        log_id: str,
        expected_head: Position | None,
        records: tuple[ReplicatedRecord, ...],
    ) -> AppendResult:
        raise NotImplementedError


def _records(count: int) -> tuple[ReplicatedRecord, ...]:
    records: list[ReplicatedRecord] = []
    parent = None
    for index in range(count):
        data = {"index": index}
        record = ReplicatedRecord(
            record_id_of("test", parent, (), data),
            "test",
            parent,
            (),
            data,
        )
        records.append(record)
        parent = record.position
    return tuple(records)


def main() -> int:
    records = _records(129)
    store = MemoryStore(records)
    assert read_all(store, "test") == records
    assert find_record(store, records[-1].position) == records[-1]
    assert find_record(store, Position("test", "missing")) is None

    try:
        read_all(MemoryStore((), stalled=True), "test")
    except ValueError as error:
        assert str(error) == "test returned an empty continuation page"
    else:
        raise AssertionError("stalled pagination was accepted")

    assert strict_json_loads('{"value":1}') == {"value": 1}
    for invalid in ('{"value":1,"value":2}', '{"value":NaN}'):
        try:
            strict_json_loads(invalid)
        except InvalidReplicaData:
            pass
        else:
            raise AssertionError(f"invalid JSON was accepted: {invalid}")
    try:
        b"\xff".decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("invalid UTF-8 was accepted")
    try:
        strict_json_loads("{")
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("malformed JSON was accepted")

    class DomainError(ValueError):
        pass

    assert require_text(" ", "value") == " "
    assert require_position(
        {"logId": " ", "recordId": "record"}, "position"
    ) == Position(" ", "record")
    for validate, value in (
        (require_non_blank_text, " "),
        (
            require_non_blank_position,
            {"logId": "log", "recordId": " "},
        ),
    ):
        try:
            validate(value, "value", error_type=DomainError)
        except DomainError as error:
            assert str(error).endswith("must be non-empty")
        else:
            raise AssertionError("blank text was accepted")

    print("shared Log reads and strict JSON validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
