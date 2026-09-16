"""Authorization tests for reading Playground commits and student Work.

The tests build real Git history in a private bare repository and real database
records, then exercise every allow path and the near-misses the contract names.
"""

import json
import os
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

import accounts
import authorization
import bundles
import classes
import playgrounds
import releases
import repository
from database import open_database


PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)
FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)
BEFORE = datetime(2019, 1, 1, tzinfo=timezone.utc)
AFTER = datetime(2999, 1, 2, tzinfo=timezone.utc)


class AuthorizationTestBase(unittest.TestCase):
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
        self.admin_actor = accounts.create_user(
            self.connection,
            bootstrap,
            f"admin-{uuid.uuid4().hex[:8]}",
            "secret",
            "admin",
        )

    # ----- database fixtures ---------------------------------------------

    def create_user(self, role):
        return accounts.create_user(
            self.connection,
            self.admin_actor,
            f"{role}-{uuid.uuid4().hex[:8]}",
            "secret",
            role,
        )

    def make_class(self, teacher, name="Class"):
        return classes.create_class(
            self.connection, self.admin_actor, name, teacher.id
        )

    def assign_student(self, student, class_id):
        return classes.assign_student(
            self.connection, self.admin_actor, student.id, class_id
        )

    def create_playground(self, creator, playground_id):
        """Create Playground metadata; management is Admin-only."""
        return playgrounds.create_playground(
            self.connection, self.admin_actor, playground_id
        )

    def make_public(self, creator, playground_id):
        return playgrounds.update_playground(
            self.connection, self.admin_actor, playground_id,
            {"policy": "public"},
        )

    def archive(self, creator, playground_id):
        return playgrounds.update_playground(
            self.connection, self.admin_actor, playground_id,
            {"archived": True},
        )

    def make_bundle(self, course_data=b"print(1)"):
        return bundles.validate_bundle(
            [
                bundles.BundleEntry(
                    "picopg.json",
                    b'{"schemaVersion":1,"runtime":"python"}',
                ),
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

    def publish(self, actor, playground_id, course_data=b"print(1)"):
        """Publish as Admin; Playground publishing is Admin-only."""
        return playgrounds.publish_playground(
            self.connection,
            self.admin_actor,
            playground_id,
            self.repository,
            self.make_bundle(course_data),
            self.metadata(f"publish {playground_id}"),
        )

    def release_for(
        self,
        teacher,
        class_id,
        playground_id,
        commit_id,
        open_at=PAST,
        key=None,
    ):
        status, body = releases.create_release(
            self.connection,
            teacher,
            self.repository,
            class_id,
            playground_id,
            commit_id,
            open_at,
            False,
            key or uuid.uuid4().hex,
        )
        self.assertEqual(status, 201)
        return body["release"]

    def insert_submission(self, release_id, student_id, work_commit_id):
        submission_id = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO submissions
                    (id, release_id, student_id, work_commit_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    release_id,
                    student_id,
                    work_commit_id,
                    "2026-01-01T00:00:00.000000Z",
                ),
            )
        return submission_id

    def submission_lookup(self, connection, student_id, work_commit_id):
        rows = connection.execute(
            """
            SELECT release_id FROM submissions
            WHERE student_id = ? AND work_commit_id = ?
            """,
            (student_id, work_commit_id),
        ).fetchall()
        return [row["release_id"] for row in rows]

    # ----- Git fixtures ---------------------------------------------------

    def git(self, *arguments, input=None, environment=None):
        effective = dict(os.environ)
        if environment is not None:
            effective.update(environment)
        completed = subprocess.run(
            ["git", "-C", str(self.repository_path), *arguments],
            input=input,
            capture_output=True,
            env=effective,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", "replace"),
        )
        return completed.stdout

    def hash_blob(self, data):
        return self.git(
            "hash-object", "-w", "--stdin", input=data
        ).strip().decode("ascii")

    def write_tree(self, files):
        root = {}
        for path, data in files.items():
            node = root
            parts = path.split("/")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = data
        return self._write_tree_node(root)

    def _write_tree_node(self, node):
        lines = []
        for name in sorted(node):
            value = node[name]
            if isinstance(value, dict):
                object_id = self._write_tree_node(value)
                lines.append(f"040000 tree {object_id}\t{name}")
            else:
                object_id = self.hash_blob(value)
                lines.append(f"100644 blob {object_id}\t{name}")
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        return self.git("mktree", input=payload).strip().decode("ascii")

    def commit_tree(self, files, message, parent=None):
        tree = self.write_tree(files)
        arguments = ["commit-tree", tree]
        if parent is not None:
            arguments += ["-p", parent]
        arguments += ["-m", message]
        environment = {
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.test",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "author@example.test",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
        }
        return (
            self.git(*arguments, environment=environment)
            .strip()
            .decode("ascii")
        )

    def work_commit(
        self,
        playground_id,
        student_id,
        base_commit_id,
        work_data=b"print('work')",
        message="work",
        parent=None,
        manifest=None,
    ):
        if manifest is None:
            manifest = json.dumps(
                {"playgroundCommitId": base_commit_id}
            ).encode("utf-8")
        files = {".picopg/work.json": manifest, "work/main.py": work_data}
        commit_id = self.commit_tree(files, message, parent=parent)
        self.git(
            "update-ref",
            self.repository.work_ref(playground_id, student_id),
            commit_id,
        )
        return commit_id

    def commit_without_manifest(self, playground_id, student_id, parent):
        files = {"work/main.py": b"print('work')"}
        commit_id = self.commit_tree(files, "no manifest", parent=parent)
        self.git(
            "update-ref",
            self.repository.work_ref(playground_id, student_id),
            commit_id,
        )
        return commit_id

    # ----- decision helpers ----------------------------------------------

    def assert_allowed(self, actor, playground_id, commit_id, **kwargs):
        self.assertIsNone(
            authorization.authorize_content_read(
                self.connection,
                actor,
                self.repository,
                playground_id,
                commit_id,
                **kwargs,
            )
        )

    def assert_denied(self, actor, playground_id, commit_id, **kwargs):
        with self.assertRaises(authorization.ContentAccessDeniedError):
            authorization.authorize_content_read(
                self.connection,
                actor,
                self.repository,
                playground_id,
                commit_id,
                **kwargs,
            )

    def denial(self, actor, playground_id, commit_id, **kwargs):
        with self.assertRaises(
            authorization.ContentAccessDeniedError
        ) as caught:
            authorization.authorize_content_read(
                self.connection,
                actor,
                self.repository,
                playground_id,
                commit_id,
                **kwargs,
            )
        return caught.exception

    def teacher_class_playground(self, playground_id="pg-main"):
        teacher = self.create_user("teacher")
        klass = self.make_class(teacher, f"Class {uuid.uuid4().hex[:8]}")
        self.create_playground(teacher, playground_id)
        published = self.publish(teacher, playground_id)
        return teacher, klass, playground_id, published.commit_id


class PlaygroundCommitReadTest(AuthorizationTestBase):
    def test_release_only_playground_without_release_is_denied(self):
        teacher, _, playground_id, commit_id = self.teacher_class_playground()
        unrelated = self.create_user("student")

        self.assert_denied(unrelated, playground_id, commit_id)

    def test_public_unarchived_playground_is_readable_by_unrelated_actor(self):
        teacher, _, playground_id, commit_id = self.teacher_class_playground()
        self.make_public(teacher, playground_id)
        unrelated_student = self.create_user("student")
        unrelated_teacher = self.create_user("teacher")

        self.assert_allowed(unrelated_student, playground_id, commit_id)
        self.assert_allowed(unrelated_teacher, playground_id, commit_id)

    def test_archived_public_playground_is_denied(self):
        teacher, _, playground_id, commit_id = self.teacher_class_playground()
        self.make_public(teacher, playground_id)
        self.archive(teacher, playground_id)
        unrelated = self.create_user("student")

        self.assert_denied(unrelated, playground_id, commit_id)

    def test_student_current_class_open_release_allows_exact_commit(self):
        teacher, klass, playground_id, commit_id = (
            self.teacher_class_playground()
        )
        student = self.create_user("student")
        self.assign_student(student, klass.id)
        self.release_for(teacher, klass.id, playground_id, commit_id)

        self.assert_allowed(student, playground_id, commit_id)

    def test_student_release_for_wrong_commit_is_denied(self):
        teacher, klass, playground_id, first_commit = (
            self.teacher_class_playground()
        )
        second_commit = self.publish(
            teacher, playground_id, b"print(2)"
        ).commit_id
        student = self.create_user("student")
        self.assign_student(student, klass.id)
        self.release_for(teacher, klass.id, playground_id, first_commit)

        self.assert_denied(student, playground_id, second_commit)
        self.assert_allowed(student, playground_id, first_commit)

    def test_student_release_in_another_class_is_denied(self):
        teacher, klass, playground_id, commit_id = (
            self.teacher_class_playground()
        )
        other_teacher, other_class, _, _ = self.teacher_class_playground(
            "pg-other"
        )
        student = self.create_user("student")
        self.assign_student(student, klass.id)
        self.release_for(
            other_teacher, other_class.id, playground_id, commit_id
        )

        self.assert_denied(student, playground_id, commit_id)

    def test_future_release_is_denied_until_open(self):
        teacher, klass, playground_id, commit_id = (
            self.teacher_class_playground()
        )
        student = self.create_user("student")
        self.assign_student(student, klass.id)
        self.release_for(
            teacher, klass.id, playground_id, commit_id, open_at=FUTURE
        )

        self.assert_denied(student, playground_id, commit_id)
        self.assert_denied(student, playground_id, commit_id, now=BEFORE)
        self.assert_denied(
            student, playground_id, commit_id, now="2019-01-01T00:00:00Z"
        )
        self.assert_allowed(student, playground_id, commit_id, now=AFTER)

    def test_transferred_student_loses_release_access(self):
        teacher, klass, playground_id, commit_id = (
            self.teacher_class_playground()
        )
        other_teacher = self.create_user("teacher")
        other_class = self.make_class(other_teacher, "Other class")
        student = self.create_user("student")
        self.assign_student(student, klass.id)
        self.release_for(teacher, klass.id, playground_id, commit_id)

        self.assert_allowed(student, playground_id, commit_id)

        self.assign_student(student, other_class.id)
        self.assert_denied(student, playground_id, commit_id)

    def test_student_own_work_reference_allows_pinned_commit(self):
        teacher, _, playground_id, commit_id = self.teacher_class_playground()
        student = self.create_user("student")
        self.work_commit(
            playground_id, student.id, commit_id, parent=commit_id
        )

        self.assert_allowed(student, playground_id, commit_id)

    def test_student_own_work_reference_is_commit_specific(self):
        teacher, _, playground_id, first_commit = (
            self.teacher_class_playground()
        )
        second_commit = self.publish(
            teacher, playground_id, b"print(2)"
        ).commit_id
        student = self.create_user("student")
        self.work_commit(
            playground_id, student.id, first_commit, parent=first_commit
        )

        self.assert_allowed(student, playground_id, first_commit)
        self.assert_denied(student, playground_id, second_commit)

    def test_other_students_work_does_not_authorize(self):
        teacher, _, playground_id, commit_id = self.teacher_class_playground()
        owner = self.create_user("student")
        other = self.create_user("student")
        self.work_commit(playground_id, owner.id, commit_id, parent=commit_id)

        self.assert_denied(other, playground_id, commit_id)

    def test_malformed_work_manifest_denies_own_work_path(self):
        teacher, _, playground_id, commit_id = self.teacher_class_playground()
        student = self.create_user("student")
        self.work_commit(
            playground_id,
            student.id,
            commit_id,
            parent=commit_id,
            manifest=b"not json",
        )

        self.assert_denied(student, playground_id, commit_id)

    def test_missing_work_manifest_denies_own_work_path(self):
        teacher, _, playground_id, commit_id = self.teacher_class_playground()
        student = self.create_user("student")
        self.commit_without_manifest(playground_id, student.id, commit_id)

        self.assert_denied(student, playground_id, commit_id)

    def test_teacher_owning_class_with_release_is_allowed(self):
        self.create_playground(None, "pg-shared")
        commit_id = self.publish(None, "pg-shared").commit_id

        teacher = self.create_user("teacher")
        klass = self.make_class(teacher, "Class shared")
        self.release_for(teacher, klass.id, "pg-shared", commit_id)

        self.assert_allowed(teacher, "pg-shared", commit_id)

    def test_unrelated_teacher_is_denied(self):
        teacher, klass, playground_id, commit_id = (
            self.teacher_class_playground()
        )
        self.release_for(teacher, klass.id, playground_id, commit_id)
        unrelated = self.create_user("teacher")

        self.assert_denied(unrelated, playground_id, commit_id)

    def test_teacher_release_for_other_commit_is_denied(self):
        self.create_playground(None, "pg-shared")
        first_commit = self.publish(None, "pg-shared").commit_id
        second_commit = self.publish(
            None, "pg-shared", b"print(2)"
        ).commit_id

        teacher = self.create_user("teacher")
        klass = self.make_class(teacher, "Class shared")
        self.release_for(teacher, klass.id, "pg-shared", first_commit)

        self.assert_allowed(teacher, "pg-shared", first_commit)
        self.assert_denied(teacher, "pg-shared", second_commit)

    def test_admin_is_allowed(self):
        _, _, playground_id, commit_id = self.teacher_class_playground()

        self.assert_allowed(self.admin_actor, playground_id, commit_id)

    def test_missing_playground_is_denied(self):
        actor = self.create_user("student")

        self.assert_denied(actor, "pg-missing", "a" * 40)

    def test_symbolic_commit_name_is_denied(self):
        teacher, _, playground_id, _ = self.teacher_class_playground()
        self.make_public(teacher, playground_id)
        actor = self.create_user("student")

        for commit_id in ("HEAD", "refs/heads/playgrounds/" + playground_id,
                          "main", "A" * 40, "abc"):
            with self.subTest(commit_id=commit_id):
                self.assert_denied(actor, playground_id, commit_id)

    def test_commit_from_another_playground_is_denied_even_when_public(self):
        teacher, _, public_playground, public_commit = (
            self.teacher_class_playground("pg-public")
        )
        self.make_public(teacher, public_playground)
        _, _, other_playground, other_commit = (
            self.teacher_class_playground("pg-other")
        )
        actor = self.create_user("student")

        self.assert_denied(actor, public_playground, other_commit)
        self.assert_denied(actor, other_playground, public_commit)

    def test_public_playground_does_not_expose_work_commit(self):
        teacher, _, playground_id, commit_id = self.teacher_class_playground()
        self.make_public(teacher, playground_id)
        student = self.create_user("student")
        work_commit = self.work_commit(
            playground_id, student.id, commit_id, parent=commit_id
        )
        unrelated = self.create_user("student")

        self.assert_denied(unrelated, playground_id, work_commit)

    def test_missing_and_unauthorized_share_one_denial(self):
        teacher, _, playground_id, commit_id = self.teacher_class_playground()
        unauthorized = self.create_user("student")

        missing = self.denial(unauthorized, "pg-missing", commit_id)
        denied = self.denial(unauthorized, playground_id, commit_id)

        self.assertEqual(type(missing), type(denied))
        self.assertEqual(str(missing), str(denied))
        self.assertEqual(missing.args, denied.args)

    def test_non_user_actor_is_denied(self):
        _, _, playground_id, commit_id = self.teacher_class_playground()

        for actor in (None, "user", 7):
            with self.subTest(actor=actor):
                self.assert_denied(actor, playground_id, commit_id)

    def test_invalid_repository_and_time_are_rejected(self):
        actor = self.create_user("student")
        with self.assertRaises(
            authorization.InvalidAuthorizationRequestError
        ):
            authorization.authorize_content_read(
                self.connection,
                actor,
                "not-a-repository",
                "pg",
                "a" * 40,
            )
        with self.assertRaises(
            authorization.InvalidAuthorizationRequestError
        ):
            authorization.authorize_content_read(
                self.connection,
                actor,
                self.repository,
                "pg",
                "a" * 40,
                now=datetime(2020, 1, 1),
            )


class WorkReadTest(AuthorizationTestBase):
    def setUp(self):
        super().setUp()
        self.teacher, self.klass, self.playground_id, self.base_commit = (
            self.teacher_class_playground()
        )
        self.release = self.release_for(
            self.teacher, self.klass.id, self.playground_id, self.base_commit
        )
        self.student = self.create_user("student")
        self.assign_student(self.student, self.klass.id)
        self.first_work = self.work_commit(
            self.playground_id,
            self.student.id,
            self.base_commit,
            work_data=b"print(1)",
            parent=self.base_commit,
        )
        self.second_work = self.work_commit(
            self.playground_id,
            self.student.id,
            self.base_commit,
            work_data=b"print(2)",
            message="work 2",
            parent=self.first_work,
        )

    def test_student_reads_own_work_head(self):
        self.assert_allowed(
            self.student,
            self.playground_id,
            self.second_work,
            work_student_id=self.student.id,
        )

    def test_student_reads_older_own_work_commit(self):
        self.assert_allowed(
            self.student,
            self.playground_id,
            self.first_work,
            work_student_id=self.student.id,
        )

    def test_other_student_cannot_read_work(self):
        other = self.create_user("student")
        self.assert_denied(
            other,
            self.playground_id,
            self.second_work,
            work_student_id=self.student.id,
        )

    def test_teacher_reads_submitted_work_under_own_class(self):
        self.insert_submission(
            self.release["id"], self.student.id, self.second_work
        )

        self.assert_allowed(
            self.teacher,
            self.playground_id,
            self.second_work,
            work_student_id=self.student.id,
            submission_lookup=self.submission_lookup,
        )

    def test_teacher_without_submission_is_denied(self):
        self.assert_denied(
            self.teacher,
            self.playground_id,
            self.second_work,
            work_student_id=self.student.id,
            submission_lookup=self.submission_lookup,
        )

    def test_teacher_without_lookup_hook_is_denied(self):
        self.insert_submission(
            self.release["id"], self.student.id, self.second_work
        )

        self.assert_denied(
            self.teacher,
            self.playground_id,
            self.second_work,
            work_student_id=self.student.id,
        )

    def test_submission_for_other_commit_does_not_authorize(self):
        self.insert_submission(
            self.release["id"], self.student.id, self.first_work
        )

        self.assert_allowed(
            self.teacher,
            self.playground_id,
            self.first_work,
            work_student_id=self.student.id,
            submission_lookup=self.submission_lookup,
        )
        self.assert_denied(
            self.teacher,
            self.playground_id,
            self.second_work,
            work_student_id=self.student.id,
            submission_lookup=self.submission_lookup,
        )

    def test_submission_under_another_class_is_denied(self):
        other_teacher = self.create_user("teacher")
        other_class = self.make_class(other_teacher, "Other class")
        other_release = self.release_for(
            other_teacher,
            other_class.id,
            self.playground_id,
            self.base_commit,
        )
        self.insert_submission(
            other_release["id"], self.student.id, self.second_work
        )

        self.assert_denied(
            self.teacher,
            self.playground_id,
            self.second_work,
            work_student_id=self.student.id,
            submission_lookup=self.submission_lookup,
        )
        self.assert_allowed(
            other_teacher,
            self.playground_id,
            self.second_work,
            work_student_id=self.student.id,
            submission_lookup=self.submission_lookup,
        )

    def test_teacher_access_is_rechecked_every_call(self):
        submission_id = self.insert_submission(
            self.release["id"], self.student.id, self.second_work
        )
        self.assert_allowed(
            self.teacher,
            self.playground_id,
            self.second_work,
            work_student_id=self.student.id,
            submission_lookup=self.submission_lookup,
        )

        with self.connection:
            self.connection.execute(
                "DELETE FROM submissions WHERE id = ?", (submission_id,)
            )

        self.assert_denied(
            self.teacher,
            self.playground_id,
            self.second_work,
            work_student_id=self.student.id,
            submission_lookup=self.submission_lookup,
        )

    def test_admin_work_read_is_denied(self):
        self.insert_submission(
            self.release["id"], self.student.id, self.second_work
        )

        self.assert_denied(
            self.admin_actor,
            self.playground_id,
            self.second_work,
            work_student_id=self.student.id,
            submission_lookup=self.submission_lookup,
        )

    def test_commit_outside_student_work_history_is_denied(self):
        _, _, other_playground, other_commit = (
            self.teacher_class_playground("pg-other")
        )

        self.assert_denied(
            self.student,
            self.playground_id,
            other_commit,
            work_student_id=self.student.id,
        )

    def test_missing_work_ref_is_denied(self):
        student_without_work = self.create_user("student")

        self.assert_denied(
            student_without_work,
            self.playground_id,
            self.second_work,
            work_student_id=student_without_work.id,
        )

    def test_student_cannot_claim_another_student_id(self):
        other = self.create_user("student")
        self.assert_denied(
            self.student,
            self.playground_id,
            self.second_work,
            work_student_id=other.id,
        )


if __name__ == "__main__":
    unittest.main()
