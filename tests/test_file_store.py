"""The file store: one append-only file per Log, crash tails, and migration from the single-document format."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from picopg.replica import MissingDependency, Position, ReplicatedRecord, UnknownPosition, record_id_of
from picopg.replica.file_store import FileReplicaLogStore, ReplicaLogFileError


def _record(log_id: str, parent: Position | None, data: object, requires: tuple[Position, ...] = ()) -> ReplicatedRecord:
    return ReplicatedRecord(record_id_of(log_id, parent, requires, data), log_id, parent, requires, data)


def _chain(log_id: str, count: int, start: Position | None = None) -> tuple[ReplicatedRecord, ...]:
    records, parent = [], start
    for index in range(count):
        record = _record(log_id, parent, {"index": index, "text": f"第 {index} 条"})
        records.append(record)
        parent = record.position
    return tuple(records)


class FileStoreTest(unittest.TestCase):
    def test_appends_survive_reopening_and_reads_page_by_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileReplicaLogStore(root)
            first = _chain("principal/a/draft/x/revision/r", 3)
            assert store.append("principal/a/draft/x/revision/r", None, first).kind == "accepted"
            more = _chain("principal/a/draft/x/revision/r", 2, first[-1].position)
            assert store.append("principal/a/draft/x/revision/r", first[-1].position, more).kind == "accepted"
            other = _record("principal/a/submission/r/requests", None, {"k": 1}, (first[0].position,))
            assert store.append(other.log_id, None, (other,)).kind == "accepted"
            assert len(list((root / "logs").glob("*.jsonl"))) == 2, "one file per Log"

            reopened = FileReplicaLogStore(root)
            log = "principal/a/draft/x/revision/r"
            assert reopened.head(log) == more[-1].position
            assert reopened.has(first[1].position) and not reopened.has(Position(log, "sha256:missing"))
            page = reopened.read(log, first[1].position, 2)
            assert [record.id for record in page.records] == [first[2].id, more[0].id] and page.has_more
            assert reopened.read(log, None, 10).records == (*first, *more)
            with self.assertRaises(UnknownPosition):
                reopened.read(log, Position(log, "sha256:missing"), 1)
            # 重放同一批是 current，别的批次是 changed，缺依赖照旧拒绝。
            assert reopened.append(log, first[-1].position, more).kind == "current"
            assert reopened.append(log, first[-1].position, (_record(log, first[-1].position, {"other": True}),)).kind == "changed"
            with self.assertRaises(MissingDependency):
                missing = _record("other", None, {}, (Position(log, "sha256:missing"),))
                reopened.append("other", None, (missing,))

    def test_an_unacknowledged_half_written_line_is_cut_off_on_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileReplicaLogStore(root)
            records = _chain("log", 2)
            store.append("log", None, records)
            [path] = (root / "logs").glob("*.jsonl")
            with open(path, "ab") as output:
                output.write(b'{"id":"sha256:half')
            reopened = FileReplicaLogStore(root)
            assert reopened.head("log") == records[-1].position
            assert path.read_bytes().endswith(b"\n")
            after = _chain("log", 1, records[-1].position)
            assert reopened.append("log", records[-1].position, after).kind == "accepted"
            assert FileReplicaLogStore(root).head("log") == after[0].position

    def test_a_single_document_store_is_migrated_once_and_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a = _chain("a", 3)
            b = _record("b", None, {"needs": "a"}, (a[0].position,))
            (root / "replica-log.json").write_text(json.dumps({"schema": 1, "logs": [
                {"logId": "a", "records": [record.to_value() for record in a]},
                {"logId": "b", "records": [b.to_value()]},
            ]}), encoding="utf-8")
            store = FileReplicaLogStore(root)
            assert store.log_ids() == ("a", "b")
            assert store.read("a", None, 10).records == a
            assert not (root / "replica-log.json").exists()
            assert (root / "replica-log.json.migrated").exists()
            assert FileReplicaLogStore(root).head("b") == b.position

    def test_a_broken_single_document_is_not_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "replica-log.json").write_text('{"schema": 1, "logs": [{"logId": "a"}]}', encoding="utf-8")
            with self.assertRaises(ReplicaLogFileError):
                FileReplicaLogStore(root)
            assert (root / "replica-log.json").exists() and not (root / "logs").exists()


if __name__ == "__main__":
    unittest.main()
