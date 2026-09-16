"""Tests for immutable Submission creation, reads, and the auth lookup.

The tests build real Playground history, Work commits, Classes, Releases, and a
real database, then exercise membership, open/due boundaries, the exact
Work/Release pin relationship, idempotent retries, every read role, and the
narrow lookup the centralized authorization uses.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Allow ``import submissions`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import accounts  # noqa: E402
import authorization  # noqa: E402
import bundles  # noqa: E402
import classes  # noqa: E402
import idempotency  # noqa: E402
import playgrounds  # noqa: E402
import releases  # noqa: E402
import repository  # noqa: E402
import submissions  # noqa: E402
import works  # noqa: E402
from database import open_database  # noqa: E402


PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)
FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)
BEFORE = datetime(2019, 1, 1, tzinfo=timezone.utc)
AFTER = datetime(2999, 1, 2, tzinfo=timezone.utc)
DUE = datetime(2020, 1, 8, tzinfo=timezone.utc)
JUST_BEFORE_DUE = datetime(2020, 1, 7, 23, 59, 59, tzinfo=timezone.utc)
JUST_AFTER_DUE = datetime(2020, 1, 8, 0, 0, 1, tzinfo=timezone.utc)
MANIFEST_BYTES = b'{"schemaVersion":1,"runtime":"python"}'

_UNSET = object()


class SubmissionTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        base = Path(self.temporary_directory.name)
        self.connection = open_database(base / "app.db")
        self.addCleanup(self.connection.close)
        self.repository_path = base / "picopg.git"
        self.repository = repository.open_repository(self.repository_path)

        bootstrap = accounts.User(
            id="seed-actor",
            username="seed-actor",
            role="admin",
            created_at="2026-01-01T00:00:00Z",
        )
        self.admin = accounts.create_user(
            self.connection,
            bootstrap,
            f"admin-{uuid.uuid4().hex[:8]}",
            "secret",
            "admin",
        )
        self.teacher = self.create_user("teacher")
        self.student = self.create_user("student")
        self.other_student = self.create_user("student")
        self.klass = classes.create_class(
            self.connection, self.admin, "Class", self.teacher.id
        )

        self.playground_id = "pg-sub"
        self.create_playground(self.playground_id)
        self.first = self.publish(self.playground_id, b"print(1)")
        self.assign_student(self.student)
        self.release = self.release_for()
        self.work_commit_id = self.create_work()

    # ----- database helpers ---------------------------------------------

    def create_user(self, role):
        return accounts.create_user(
            self.connection,
            self.admin,
            f"{role}-{uuid.uuid4().hex[:8]}",
            "secret",
            role,
        )

    def assign_student(self, student, class_id=None):
        return classes.assign_student(
            self.connection,
            self.admin,
            student.id,
            class_id or self.klass.id,
        )

    def create_playground(self, playground_id):
        return playgrounds.create_playground(
            self.connection, self.admin, playground_id
        )

    def make_bundle(self, course_data=b"print(1)"):
        return bundles.validate_bundle(
            [
                bundles.BundleEntry("picopg.json", MANIFEST_BYTES),
                bundles.BundleEntry("course/module/app.py", course_data),
                bundles.BundleEntry("starter/setup.py", b"setup"),
            ]
        )

    def metadata(self, message="publish playground"):
        return repository.CommitMetadata(
            name="Test Author",
            email="author@example.test",
            timestamp="2026-01-01T00:00:00+00:00",
            message=message,
        )

    def publish(self, playground_id, course_data=b"print(1)"):
        return playgrounds.publish_playground(
            self.connection,
            self.admin,
            playground_id,
            self.repository,
            self.make_bundle(course_data),
            self.metadata(f"publish {playground_id}"),
        )

    def release_for(
        self,
        teacher=None,
        class_id=None,
        playground_id=None,
        commit_id=None,
        open_at=PAST,
        due_at=None,
        requires_submission=False,
        key=None,
    ):
        teacher = self.teacher if teacher is None else teacher
        class_id = self.klass.id if class_id is None else class_id
        playground_id = (
            self.playground_id if playground_id is None else playground_id
        )
        commit_id = self.first.commit_id if commit_id is None else commit_id
        status, body = releases.create_release(
            self.connection,
            teacher,
            self.repository,
            class_id,
            playground_id,
            commit_id,
            open_at,
            requires_submission,
            key or uuid.uuid4().hex,
            due_at=due_at,
        )
        self.assertEqual(status, 201)
        return body["release"]

    # ----- Work helpers --------------------------------------------------

    def work_files(self, files):
        if files is None:
            files = {"work/main.py": b"print('work')"}
        return works.validate_work_files(
            [works.WorkEntry(path, data) for path, data in files.items()]
        )

    def create_work(
        self,
        actor=None,
        playground_commit_id=None,
        files=None,
        base_commit_id=None,
    ):
        actor = self.student if actor is None else actor
        playground_commit_id = (
            self.first.commit_id
            if playground_commit_id is None
            else playground_commit_id
        )
        return works.create_work_commit(
            self.connection,
            actor,
            self.repository,
            self.playground_id,
            playground_commit_id,
            self.work_files(files),
            base_commit_id=base_commit_id,
        ).commit_id

    def work_ref(self, student_id):
        return self.repository.work_ref(self.playground_id, student_id)

    def craft_work(self, student_id, pinned, parent, data=b"crafted"):
        """Write a crafted Work commit and point the Student's ref at it."""

        manifest = json.dumps(
            {"playgroundCommitId": pinned}
        ).encode("utf-8")
        commit = self.commit_tree(
            {".picopg/work.json": manifest, "work/main.py": data},
            b"crafted\n",
            parent=parent,
        )
        self.git("update-ref", self.work_ref(student_id), commit)
        return commit

    # ----- Git helpers ---------------------------------------------------

    def git(self, *arguments, input=None, environment=None, check=True):
        effective = dict(os.environ)
        if environment is not None:
            effective.update(environment)
        completed = subprocess.run(
            ["git", "-C", str(self.repository_path), *arguments],
            input=input,
            capture_output=True,
            env=effective,
        )
        if check:
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr.decode("utf-8", "replace"),
            )
        return completed.stdout

    def commit_tree(self, files, message=b"crafted\n", parent=None):
        root = {}
        for path, data in files.items():
            node = root
            for part in path.split("/")[:-1]:
                node = node.setdefault(part, {})
            node[path.split("/")[-1]] = data
        tree = self._write_tree_node(root)
        arguments = ["commit-tree", tree]
        if parent is not None:
            arguments += ["-p", parent]
        environment = {
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.test",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "author@example.test",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
        }
        return (
            self.git(*arguments, input=message, environment=environment)
            .strip()
            .decode("ascii")
        )

    def _write_tree_node(self, node):
        lines = []
        for name in sorted(node):
            value = node[name]
            if isinstance(value, dict):
                object_id = self._write_tree_node(value)
                lines.append(f"040000 tree {object_id}\t{name}")
            else:
                blob = (
                    self.git("hash-object", "-w", "--stdin", input=value)
                    .strip()
                    .decode("ascii")
                )
                lines.append(f"100644 blob {blob}\t{name}")
        return (
            self.git("mktree", input=("\n".join(lines) + "\n").encode())
            .strip()
            .decode("ascii")
        )

    # ----- submission helpers -------------------------------------------

    def submit(
        self,
        release_id=_UNSET,
        work_commit_id=_UNSET,
        actor=_UNSET,
        key=None,
        now=_UNSET,
    ):
        actor = self.student if actor is _UNSET else actor
        release_id = (
            self.release["id"] if release_id is _UNSET else release_id
        )
        work_commit_id = (
            self.work_commit_id
            if work_commit_id is _UNSET
            else work_commit_id
        )
        now = None if now is _UNSET else now
        return submissions.create_submission(
            self.connection,
            actor,
            self.repository,
            release_id,
            work_commit_id,
            key or uuid.uuid4().hex,
            now=now,
        )

    def submission_rows(self):
        return self.connection.execute(
            "SELECT * FROM submissions ORDER BY id"
        ).fetchall()


class SubmissionCreationTest(SubmissionTestBase):
    def test_student_submits_own_work_for_open_release(self):
        status, body = self.submit()

        self.assertEqual(status, 201)
        record = body["submission"]
        self.assertEqual(record["releaseId"], self.release["id"])
        self.assertEqual(record["studentId"], self.student.id)
        self.assertEqual(record["workCommitId"], self.work_commit_id)
        self.assertTrue(record["createdAt"])

        rows = self.submission_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["release_id"], self.release["id"])
        self.assertEqual(rows[0]["student_id"], self.student.id)
        self.assertEqual(rows[0]["work_commit_id"], self.work_commit_id)

    def test_only_student_actor_may_submit(self):
        for actor in (self.teacher, self.admin, None, "user", 7):
            with self.subTest(actor=actor):
                with self.assertRaises(accounts.ForbiddenActionError):
                    self.submit(actor=actor)
        self.assertEqual(self.submission_rows(), [])

    def test_student_must_currently_be_a_member_of_the_release_class(self):
        outsider = self.create_user("student")

        with self.assertRaises(accounts.ForbiddenActionError):
            self.submit(actor=outsider)
        self.assertEqual(self.submission_rows(), [])

    def test_transferred_student_loses_the_release(self):
        self.submit()
        self.assertEqual(len(self.submission_rows()), 1)

        other_teacher = self.create_user("teacher")
        other_class = classes.create_class(
            self.connection, self.admin, "Other", other_teacher.id
        )
        self.assign_student(self.student, other_class.id)

        with self.assertRaises(accounts.ForbiddenActionError):
            self.submit()
        self.assertEqual(len(self.submission_rows()), 1)

    def test_unknown_release_is_rejected(self):
        with self.assertRaises(submissions.UnknownReleaseError):
            self.submit(release_id=uuid.uuid4().hex)
        self.assertEqual(self.submission_rows(), [])

    def test_work_commit_must_be_on_the_students_own_history(self):
        # A real Work commit owned by another Student is not this Student's.
        self.assign_student(self.other_student)
        other_work = self.create_work(
            actor=self.other_student,
            files={"work/main.py": b"other"},
        )
        self.assertNotEqual(other_work, self.work_commit_id)

        with self.assertRaises(submissions.UnknownWorkCommitError):
            self.submit(work_commit_id=other_work)
        self.assertEqual(self.submission_rows(), [])

    def test_commit_outside_the_work_history_is_rejected(self):
        unreachable = self.commit_tree(
            {"work/main.py": b"x"}, parent=self.first.commit_id
        )
        with self.assertRaises(submissions.UnknownWorkCommitError):
            self.submit(work_commit_id=unreachable)
        self.assertEqual(self.submission_rows(), [])

    def test_work_commit_must_pin_the_release_playground_commit(self):
        second = self.publish(self.playground_id, b"print(2)")
        second_release = self.release_for(commit_id=second.commit_id)

        # The Student's Work commit pins the first Playground commit, but the
        # Release fixes the second: the exact pin must match the Release.
        with self.assertRaises(submissions.UnknownWorkCommitError):
            self.submit(
                release_id=second_release["id"],
                work_commit_id=self.work_commit_id,
            )
        self.assertEqual(self.submission_rows(), [])

    def test_wrong_pin_is_rejected_even_when_the_release_matches_head(self):
        second = self.publish(self.playground_id, b"print(2)")
        # Head pins the release commit but the supplied commit pins another
        # Playground commit: only the exact supplied commit is checked.
        x = self.craft_work(
            self.student.id, second.commit_id, self.first.commit_id
        )
        self.craft_work(self.student.id, self.first.commit_id, x)

        with self.assertRaises(submissions.UnknownWorkCommitError):
            self.submit(work_commit_id=x)
        self.assertEqual(self.submission_rows(), [])

    def test_supplied_commit_not_work_head_is_used(self):
        second = self.publish(self.playground_id, b"print(2)")
        # X pins the release's Playground commit; its descendant Y pins a
        # different commit and is the Work HEAD.  Submitting X must succeed,
        # which only holds if the exact supplied commit is read rather than
        # the ref's HEAD.
        x = self.craft_work(
            self.student.id, self.first.commit_id, self.first.commit_id
        )
        self.craft_work(self.student.id, second.commit_id, x)

        status, body = self.submit(work_commit_id=x)
        self.assertEqual(status, 201)
        self.assertEqual(body["submission"]["workCommitId"], x)
        self.assertEqual(self.submission_rows()[0]["work_commit_id"], x)

    def test_malformed_request_fields_are_rejected(self):
        for work_commit_id in ("", "abc", "HEAD", "A" * 40, None, 7):
            with self.subTest(work_commit_id=work_commit_id):
                with self.assertRaises(
                    submissions.InvalidSubmissionRequestError
                ):
                    self.submit(work_commit_id=work_commit_id)

        for release_id in ("", None, 7):
            with self.subTest(release_id=release_id):
                with self.assertRaises(
                    submissions.InvalidSubmissionRequestError
                ):
                    self.submit(release_id=release_id)

        for now in (7, "", "not-a-time", datetime(2020, 1, 1)):
            with self.subTest(now=now):
                with self.assertRaises(
                    submissions.InvalidSubmissionRequestError
                ):
                    self.submit(now=now)
        self.assertEqual(self.submission_rows(), [])

    def test_invalid_repository_is_rejected(self):
        with self.assertRaises(submissions.InvalidSubmissionRequestError):
            submissions.create_submission(
                self.connection,
                self.student,
                "not-a-repository",
                self.release["id"],
                self.work_commit_id,
                uuid.uuid4().hex,
            )

    def test_submission_is_frozen_and_has_no_mutation_api(self):
        import dataclasses

        record = submissions.Submission(
            id="s",
            release_id="r",
            student_id="u",
            work_commit_id="a" * 40,
            created_at="2026-01-01T00:00:00.000000Z",
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.work_commit_id = "b" * 40

        for name in (
            "update_submission",
            "delete_submission",
            "patch_submission",
            "set_submission_status",
        ):
            self.assertFalse(hasattr(submissions, name))


class SubmissionBoundaryTest(SubmissionTestBase):
    def test_future_release_is_rejected_until_it_opens(self):
        future = self.release_for(open_at=FUTURE)

        with self.assertRaises(submissions.SubmissionNotAllowedError):
            self.submit(release_id=future["id"], now=BEFORE)
        self.assertEqual(self.submission_rows(), [])

        # Exactly at the open instant and afterwards it is allowed.
        status, _ = self.submit(release_id=future["id"], now=FUTURE)
        self.assertEqual(status, 201)
        self.assertEqual(len(self.submission_rows()), 1)

    def test_required_submission_rejects_after_due(self):
        required = self.release_for(
            due_at=DUE, requires_submission=True
        )

        status, _ = self.submit(
            release_id=required["id"], now=JUST_BEFORE_DUE
        )
        self.assertEqual(status, 201)

        with self.assertRaises(submissions.SubmissionNotAllowedError):
            self.submit(release_id=required["id"], now=JUST_AFTER_DUE)
        self.assertEqual(len(self.submission_rows()), 1)

    def test_required_submission_at_exact_due_is_allowed(self):
        required = self.release_for(
            due_at=DUE, requires_submission=True
        )
        status, _ = self.submit(release_id=required["id"], now=DUE)
        self.assertEqual(status, 201)
        self.assertEqual(len(self.submission_rows()), 1)

    def test_optional_submission_is_allowed_after_due(self):
        optional = self.release_for(
            due_at=DUE, requires_submission=False
        )
        status, _ = self.submit(
            release_id=optional["id"], now=JUST_AFTER_DUE
        )
        self.assertEqual(status, 201)
        self.assertEqual(len(self.submission_rows()), 1)

    def test_required_submission_without_a_due_is_open_only(self):
        required = self.release_for(requires_submission=True)

        with self.assertRaises(submissions.SubmissionNotAllowedError):
            self.submit(release_id=required["id"], now=BEFORE)

        status, _ = self.submit(release_id=required["id"], now=AFTER)
        self.assertEqual(status, 201)
        self.assertEqual(len(self.submission_rows()), 1)


class SubmissionIdempotencyTest(SubmissionTestBase):
    def test_exact_retry_replays_the_original_without_a_duplicate(self):
        key = uuid.uuid4().hex
        first = self.submit(key=key)
        second = self.submit(key=key)

        self.assertEqual(first, second)
        self.assertEqual(first[0], 201)
        self.assertEqual(len(self.submission_rows()), 1)

    def test_changed_work_commit_with_the_same_key_conflicts(self):
        key = uuid.uuid4().hex
        first_work = self.work_commit_id
        second_work = self.create_work(
            base_commit_id=first_work, files={"work/main.py": b"second"}
        )

        self.submit(work_commit_id=first_work, key=key)
        with self.assertRaises(idempotency.IdempotencyConflict):
            self.submit(work_commit_id=second_work, key=key)
        self.assertEqual(len(self.submission_rows()), 1)

    def test_changed_release_with_the_same_key_conflicts(self):
        key = uuid.uuid4().hex
        other_release = self.release_for()

        self.submit(release_id=self.release["id"], key=key)
        with self.assertRaises(idempotency.IdempotencyConflict):
            self.submit(release_id=other_release["id"], key=key)
        self.assertEqual(len(self.submission_rows()), 1)

    def test_a_failed_submission_records_no_result(self):
        key = uuid.uuid4().hex
        future = self.release_for(open_at=FUTURE)

        with self.assertRaises(submissions.SubmissionNotAllowedError):
            self.submit(release_id=future["id"], key=key, now=BEFORE)

        status, _ = self.submit(release_id=future["id"], key=key, now=AFTER)
        self.assertEqual(status, 201)
        self.assertEqual(len(self.submission_rows()), 1)


class SubmissionReadTest(SubmissionTestBase):
    def setUp(self):
        super().setUp()
        self.first_submission = self.submit()[1]["submission"]

        # A second Student submits under a different Teacher's Class.
        self.other_teacher = self.create_user("teacher")
        self.other_class = classes.create_class(
            self.connection, self.admin, "Elsewhere", self.other_teacher.id
        )
        self.assign_student(self.other_student, self.other_class.id)
        self.other_release = self.release_for(
            teacher=self.other_teacher, class_id=self.other_class.id
        )
        self.other_work = self.create_work(actor=self.other_student)
        self.second_submission = self.submit(
            actor=self.other_student,
            release_id=self.other_release["id"],
            work_commit_id=self.other_work,
        )[1]["submission"]

    def test_student_sees_only_own_submissions(self):
        listed = submissions.list_submissions(self.connection, self.student)
        self.assertEqual(
            [item.id for item in listed], [self.first_submission["id"]]
        )

    def test_teacher_sees_only_owned_class_submissions(self):
        listed = submissions.list_submissions(self.connection, self.teacher)
        self.assertEqual(
            {item.id for item in listed}, {self.first_submission["id"]}
        )

        other_listed = submissions.list_submissions(
            self.connection, self.other_teacher
        )
        self.assertEqual(
            {item.id for item in other_listed},
            {self.second_submission["id"]},
        )

    def test_admin_sees_all_submissions(self):
        listed = submissions.list_submissions(self.connection, self.admin)
        self.assertEqual(
            {item.id for item in listed},
            {self.first_submission["id"], self.second_submission["id"]},
        )

    def test_get_submission_visibility(self):
        submission_id = self.first_submission["id"]

        self.assertEqual(
            submissions.get_submission(
                self.connection, self.student, submission_id
            ).id,
            submission_id,
        )
        self.assertEqual(
            submissions.get_submission(
                self.connection, self.teacher, submission_id
            ).id,
            submission_id,
        )
        self.assertEqual(
            submissions.get_submission(
                self.connection, self.admin, submission_id
            ).id,
            submission_id,
        )

        other_teacher = self.create_user("teacher")
        for actor in (other_teacher, self.other_student):
            with self.subTest(role=actor.role):
                with self.assertRaises(submissions.MissingSubmissionError):
                    submissions.get_submission(
                        self.connection, actor, submission_id
                    )

    def test_get_hides_missing_and_malformed_ids(self):
        with self.assertRaises(submissions.MissingSubmissionError):
            submissions.get_submission(
                self.connection, self.student, uuid.uuid4().hex
            )
        with self.assertRaises(submissions.InvalidSubmissionRequestError):
            submissions.get_submission(self.connection, self.student, "")

    def test_invalid_actor_is_rejected_for_reads(self):
        for actor in (None, "user", 7, object()):
            with self.subTest(actor=actor):
                with self.assertRaises(accounts.ForbiddenActionError):
                    submissions.list_submissions(self.connection, actor)
                with self.assertRaises(accounts.ForbiddenActionError):
                    submissions.get_submission(
                        self.connection, actor, self.first_submission["id"]
                    )


class SubmissionLookupTest(SubmissionTestBase):
    def test_lookup_returns_release_ids_for_the_exact_commit(self):
        other_release = self.release_for()
        self.submit(release_id=self.release["id"])
        self.submit(release_id=other_release["id"])

        release_ids = submissions.release_ids_for_work_commit(
            self.connection, self.student.id, self.work_commit_id
        )
        self.assertEqual(
            sorted(release_ids),
            sorted([self.release["id"], other_release["id"]]),
        )

    def test_lookup_is_scoped_to_the_student_and_exact_commit(self):
        self.submit()

        self.assertEqual(
            submissions.release_ids_for_work_commit(
                self.connection, self.other_student.id, self.work_commit_id
            ),
            [],
        )
        self.assertEqual(
            submissions.release_ids_for_work_commit(
                self.connection, self.student.id, "a" * 40
            ),
            [],
        )
        self.assertEqual(
            submissions.release_ids_for_work_commit(
                self.connection, "", self.work_commit_id
            ),
            [],
        )
        self.assertEqual(
            submissions.release_ids_for_work_commit(
                self.connection, self.student.id, None
            ),
            [],
        )

    def test_lookup_reflects_the_real_table(self):
        # The centralized authorization reads this lookup; deleting the row
        # must make a previously authorized Teacher lose access.
        self.submit()
        self.assertIsNone(
            authorization.authorize_content_read(
                self.connection,
                self.teacher,
                self.repository,
                self.playground_id,
                self.work_commit_id,
                work_student_id=self.student.id,
            )
        )

        with self.connection:
            self.connection.execute("DELETE FROM submissions")
        self.assertEqual(
            submissions.release_ids_for_work_commit(
                self.connection, self.student.id, self.work_commit_id
            ),
            [],
        )
        with self.assertRaises(authorization.ContentAccessDeniedError):
            authorization.authorize_content_read(
                self.connection,
                self.teacher,
                self.repository,
                self.playground_id,
                self.work_commit_id,
                work_student_id=self.student.id,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
