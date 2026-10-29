from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from picopg.replica import Position, ReplicatedRecord, record_id_of
from picopg.replica.file_store import FileReplicaLogStore
from picopg.server.review.comments import CommentPublisher
from picopg.server.submissions.protocol import SubmissionError, decode_request

REVISION = Position("principal/s/draft/hanoi/revision/A", "rev-1")


def request(extra: dict) -> ReplicatedRecord:
    log_id = "principal/s/submission/A/requests"
    data = {"type": "submission.requested", "submissionId": "sub-1", "playgroundId": "hanoi",
            "revision": {"logId": REVISION.log_id, "recordId": REVISION.record_id}, **extra}
    return ReplicatedRecord(id=record_id_of(log_id, None, (REVISION,), data), log_id=log_id, parent=None, requires=(REVISION,), data=data)


class SubmissionTimeTest(unittest.TestCase):
    def test_the_device_time_rides_with_the_request_and_old_requests_still_read(self) -> None:
        assert decode_request(request({"submittedAt": "2026-10-29T06:00:00.000Z"})).submitted_at == "2026-10-29T06:00:00.000Z"
        assert decode_request(request({})).submitted_at is None
        with self.assertRaises(SubmissionError):
            decode_request(request({"submittedAt": "yesterday"}))
        with self.assertRaises(SubmissionError):
            decode_request(request({"submittedAt": "2026-10-29T06:00:00"})), "a time without a zone is ambiguous"


class FakeServer:
    def __init__(self, store) -> None:
        self.store = store

    def append_and_broadcast(self, log_id, expected_head, records):
        return self.store.append(log_id, expected_head, records)


class CommentTimeTest(unittest.TestCase):
    def test_a_comment_keeps_the_time_it_was_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = FileReplicaLogStore(Path(temporary))
            publisher = CommentPublisher(FakeServer(store), clock=lambda: "2026-10-29T06:00:00.000Z")
            publisher.add("s", "hanoi", "teacher", "看第 3 行。")
            [comment] = publisher.comments("s", "hanoi")
            assert comment.created_at == "2026-10-29T06:00:00.000Z"
            later = CommentPublisher(FakeServer(store), clock=lambda: "2030-01-01T00:00:00.000Z")
            assert later.comments("s", "hanoi")[0].created_at == "2026-10-29T06:00:00.000Z", "reading never re-stamps a fact"


class CommentReadRuleTest(unittest.TestCase):
    def test_each_device_writes_its_own_read_receipts_and_the_students_other_devices_read_them(self) -> None:
        from picopg.server.submissions.logs import SubmissionLogRule

        rule = SubmissionLogRule()
        replicas = frozenset({"A", "B"})
        assert rule.access("s", "A", replicas, "principal/s/submission/A/reads") == "append"
        assert rule.access("s", "A", replicas, "principal/s/submission/B/reads") == "read"
        assert rule.access("s", "A", replicas, "principal/s/submission/C/reads") is None, "not this student's device"
        assert rule.access("s", "A", replicas, "principal/t/submission/A/reads") is None, "never another student's"
        assert rule.access("s", "A", replicas, "principal/s/submission/B/requests") is None, "other devices' requests stay private as before"
        assert rule.owner("principal/s/submission/A/reads") == ("s", "A")
