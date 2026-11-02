import subprocess
import tempfile
import unittest
from pathlib import Path

from picopg.server.submissions.git_tags import GitSubmissionTagWriter


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(repository), *arguments], check=True, capture_output=True, text=True).stdout


class GitSubmissionTagWriterTest(unittest.TestCase):
    def test_a_freshly_initialized_repository_takes_the_first_submission_as_a_root_commit(self) -> None:
        # 部署说明与本地联调都只 git init，不做初始提交。
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            git(repository, "init", "-q")
            writer = GitSubmissionTagWriter({"hanoi": repository})
            first = writer.create(idempotency_key="a", submission_id="s1", playground_id="hanoi", revision=None, source="print(1)\n")
            second = writer.create(idempotency_key="b", submission_id="s2", playground_id="hanoi", revision=None, source="print(2)\n")
            self.assertEqual(writer.create(idempotency_key="a", submission_id="s1", playground_id="hanoi", revision=None, source="print(1)\n"), first,
                             "the same submission is idempotent")
            self.assertEqual(sorted(git(repository, "tag").split()), sorted([first, second]))
            self.assertEqual(git(repository, "rev-list", "--count", first).strip(), "1", "the first submission is a root commit")
            files = git(repository, "show", "--name-only", "--format=", second).split()
            self.assertEqual(len(files), 1)
            self.assertEqual(git(repository, "show", f"{second}:{files[0]}"), "print(2)\n")


class FailingFirstTags:
    def __init__(self) -> None:
        self.created = []

    def create(self, *, idempotency_key, submission_id, playground_id, revision, source):
        if submission_id == "broken":
            raise RuntimeError("git is unavailable for this one")
        self.created.append(submission_id)
        return f"tag/{submission_id}"


class SubmissionConsumerIsolationTest(unittest.TestCase):
    def test_one_failing_log_does_not_hold_back_other_students(self) -> None:
        from picopg.replica import ReplicatedRecord, record_id_of
        from picopg.replica.file_store import FileReplicaLogStore
        from picopg.server.drafts.protocol import revision_log_id
        from picopg.server.submissions.worker import SubmissionRequestConsumer

        def append(store, log, requires, data):
            record = ReplicatedRecord(record_id_of(log, None, requires, data), log, None, requires, data)
            assert store.append(log, None, (record,)).kind == "accepted"
            return record

        with tempfile.TemporaryDirectory() as temporary:
            store = FileReplicaLogStore(Path(temporary))
            logs = []
            for principal, submission in (("aaa", "broken"), ("bbb", "fine")):
                revision = append(store, revision_log_id(principal, "lesson", "d"), (), {
                    "type": "draft.revised", "draftId": "lesson", "replicaId": "d", "source": "x = 1\n", "base": None,
                })
                log = f"principal/{principal}/submission/d/requests"
                append(store, log, (revision.position,), {
                    "type": "submission.requested", "submissionId": submission, "playgroundId": "lesson",
                    "revision": revision.position.to_value(),
                })
                logs.append(log)
            tags = FailingFirstTags()
            consumer = SubmissionRequestConsumer(store, tuple(logs), tags)
            with self.assertRaises(RuntimeError):
                consumer.run_once()
            self.assertEqual(tags.created, ["fine"], "bbb's submission completed although aaa's sorts first and fails")


if __name__ == "__main__":
    unittest.main()
