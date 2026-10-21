"""Submission validation keeps the submitted source in its owning draft."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from apps.server.draft.protocol import revision_log_id  # noqa: E402
from apps.server.submission.worker import SubmissionRequestConsumer  # noqa: E402
from picopg.api import ReplicatedRecord, record_id_of  # noqa: E402
from picopg.impl.file_replica_log import FileReplicaLogStore  # noqa: E402


def append(store, log_id, data, requires=()):
    parent = store.head(log_id)
    record = ReplicatedRecord(
        record_id_of(log_id, parent, requires, data), log_id, parent, requires, data
    )
    assert store.append(log_id, parent, (record,)).kind == "accepted"
    return record


class Tags:
    def __init__(self):
        self.sources = []

    def create(self, **values):
        self.sources.append(values["source"])
        return "submission/test"


class SubmissionValidationTest(unittest.TestCase):
    def check_submission(self, principal, draft, accepted):
        with tempfile.TemporaryDirectory() as directory:
            store = FileReplicaLogStore(Path(directory))
            revision = append(store, revision_log_id(principal, draft, "B"), {
                "type": "draft.revised", "draftId": draft, "replicaId": "B",
                "source": "print('submitted')", "base": None,
            })
            request = append(store, "principal/student/submission/A/requests", {
                "type": "submission.requested", "submissionId": "one",
                "playgroundId": "lesson", "revision": revision.position.to_value(),
            }, (revision.position,))
            tags = Tags()
            consumer = SubmissionRequestConsumer(store, (request.log_id,), tags)
            self.assertEqual(consumer.run_once(), 1)
            result = store.read("principal/student/submission/A/results", None, 10).records[0]
            self.assertEqual(
                result.data["type"],
                "submission.completed" if accepted else "submission.failed",
            )
            self.assertEqual(tags.sources, ["print('submitted')"] if accepted else [])

    def test_revision_from_another_draft_is_rejected(self):
        self.check_submission("student", "another-lesson", False)

    def test_another_student_is_rejected(self):
        self.check_submission("another-student", "lesson", False)

    def test_same_revision_from_another_device_is_valid(self):
        self.check_submission("student", "lesson", True)


if __name__ == "__main__":
    unittest.main()
