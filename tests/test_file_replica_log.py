from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from picopg.api import (
    InvalidReplicaData,
    MissingDependency,
    Position,
    ReplicatedRecord,
)
from picopg.impl.file_replica_log import (
    FileReplicaLogStore,
    ReplicaLogFileError,
)


def record(
    log_id: str,
    record_id: str,
    parent: Position | None = None,
    requires: tuple[Position, ...] = (),
    data: object | None = None,
) -> ReplicatedRecord:
    return ReplicatedRecord(
        id=record_id,
        log_id=log_id,
        parent=parent,
        requires=requires,
        data={} if data is None else data,
    )


class FileReplicaLogStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_append_dependencies_and_restart(self) -> None:
        store = FileReplicaLogStore(self.root)
        c17 = record("work", "C17", data={"source": "print(17)"})
        accepted = store.append("work", None, (c17,))
        self.assertEqual(accepted.kind, "accepted")
        self.assertEqual(accepted.head, Position("work", "C17"))

        q4 = record(
            "requests",
            "Q4",
            requires=(Position("work", "C17"),),
            data={"type": "analysis.requested"},
        )
        store.append("requests", None, (q4,))

        reopened = FileReplicaLogStore(self.root)
        self.assertEqual(reopened.head("work"), Position("work", "C17"))
        self.assertTrue(reopened.has(Position("requests", "Q4")))
        self.assertEqual(reopened.read("requests", None, 10).records, (q4,))
        self.assertEqual(
            reopened.append("work", None, (c17,)).kind, "current"
        )
        self.assertEqual(
            reopened.append("work", None, (record("work", "fork"),)).kind,
            "changed",
        )

    def test_missing_dependency_and_corruption_are_rejected(self) -> None:
        store = FileReplicaLogStore(self.root)
        with self.assertRaises(MissingDependency):
            store.append(
                "results",
                None,
                (
                    record(
                        "results",
                        "R7",
                        requires=(Position("requests", "Q4"),),
                    ),
                ),
            )
        store.append("work", None, (record("work", "C17"),))
        path = self.root / "replica-log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["logs"][0]["records"][0]["parent"] = {
            "logId": "work",
            "recordId": "missing",
        }
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ReplicaLogFileError):
            FileReplicaLogStore(self.root)

    def test_concurrent_compare_and_append_has_one_winner(self) -> None:
        store = FileReplicaLogStore(self.root)
        first = record("work", "C17")
        store.append("work", None, (first,))
        candidates = (
            record("work", "C18", first.position),
            record("work", "branch", first.position),
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(
                executor.map(
                    lambda candidate: store.append(
                        "work", first.position, (candidate,)
                    ),
                    candidates,
                )
            )
        self.assertEqual(
            sorted(result.kind for result in results),
            ["accepted", "changed"],
        )
        self.assertEqual(len(store.read("work", None, 10).records), 2)

    def test_batch_chain_is_validated_before_write(self) -> None:
        store = FileReplicaLogStore(self.root)
        with self.assertRaises(InvalidReplicaData):
            store.append(
                "work",
                None,
                (
                    record("work", "C17"),
                    record("work", "C18", None),
                ),
            )
        self.assertIsNone(store.head("work"))


if __name__ == "__main__":
    unittest.main()
