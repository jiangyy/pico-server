"""Domain tests for immutable Release creation and role-filtered reads."""

import dataclasses
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import accounts
import bundles
import classes
import idempotency
import playgrounds
import releases
import repository
from database import open_database


PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)
FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)
BEFORE = datetime(2019, 1, 1, tzinfo=timezone.utc)
AFTER = datetime(2999, 1, 2, tzinfo=timezone.utc)


class ReleaseTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        base = Path(self.temporary_directory.name)
        self.connection = open_database(base / "app.db")
        self.addCleanup(self.connection.close)

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
        self.repository_path = base / "picopg.git"
        self.repository = repository.open_repository(self.repository_path)

    def create_user(self, role):
        return accounts.create_user(
            self.connection,
            self.admin_actor,
            f"{role}-{uuid.uuid4().hex[:8]}",
            "secret",
            role,
        )

    def make_class(self, teacher, name):
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
            self.metadata(),
        )

    def create_release(
        self,
        actor,
        class_id,
        playground_id,
        commit_id,
        request_key,
        open_at=PAST,
        due_at=None,
        requires_submission=False,
    ):
        return releases.create_release(
            self.connection,
            actor,
            self.repository,
            class_id,
            playground_id,
            commit_id,
            open_at,
            requires_submission,
            request_key,
            due_at=due_at,
        )

    def release_rows(self):
        return self.connection.execute(
            "SELECT * FROM releases ORDER BY open_at, id"
        ).fetchall()

    def ref_head(self, playground_id):
        ref = self.repository.playground_ref(playground_id)
        completed = subprocess.run(
            ["git", "-C", str(self.repository_path), "rev-parse", ref],
            capture_output=True,
            check=True,
        )
        return completed.stdout.strip().decode("ascii")

    def teacher_class_playground(self, playground_id="pg-test"):
        teacher = self.create_user("teacher")
        klass = self.make_class(teacher, f"Class {uuid.uuid4().hex[:8]}")
        self.create_playground(teacher, playground_id)
        published = self.publish(teacher, playground_id)
        return teacher, klass, playground_id, published.commit_id


class ReleaseCreationTest(ReleaseTestBase):
    def test_teacher_creates_release_with_exact_commit(self):
        teacher, klass, playground_id, commit_id = (
            self.teacher_class_playground()
        )

        status, body = self.create_release(
            teacher, klass.id, playground_id, commit_id, "key-create"
        )

        self.assertEqual(status, 201)
        record = body["release"]
        self.assertEqual(record["classId"], klass.id)
        self.assertEqual(record["playgroundId"], playground_id)
        self.assertEqual(record["playgroundCommitId"], commit_id)
        self.assertEqual(record["createdBy"], teacher.id)
        self.assertEqual(record["openAt"], "2020-01-01T00:00:00.000000Z")
        self.assertIsNone(record["dueAt"])
        self.assertFalse(record["requiresSubmission"])
        self.assertTrue(record["createdAt"])

        rows = self.release_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["playground_commit_id"], commit_id)
        self.assertEqual(rows[0]["requires_submission"], 0)

    def test_timezone_offset_is_normalized_to_utc(self):
        teacher, klass, playground_id, commit_id = (
            self.teacher_class_playground("pg-offset")
        )
        offset = timezone(timedelta(hours=5))
        open_at = datetime(2026, 1, 1, 5, 0, tzinfo=offset)

        status, body = self.create_release(
            teacher, klass.id, playground_id, commit_id, "key-offset",
            open_at=open_at,
        )

        self.assertEqual(status, 201)
        self.assertEqual(
            body["release"]["openAt"], "2026-01-01T00:00:00.000000Z"
        )

    def test_requires_submission_and_due_date_round_trip(self):
        teacher, klass, playground_id, commit_id = (
            self.teacher_class_playground("pg-required")
        )
        due_at = PAST + timedelta(days=7)

        first = self.create_release(
            teacher, klass.id, playground_id, commit_id, "key-required",
            due_at=due_at, requires_submission=True,
        )
        second = self.create_release(
            teacher, klass.id, playground_id, commit_id, "key-required",
            due_at=due_at, requires_submission=True,
        )

        self.assertEqual(first, second)
        record = first[1]["release"]
        self.assertIs(record["requiresSubmission"], True)
        self.assertEqual(record["dueAt"], "2020-01-08T00:00:00.000000Z")
        self.assertEqual(self.release_rows()[0]["requires_submission"], 1)

    def test_old_exact_commit_survives_head_advance(self):
        teacher = self.create_user("teacher")
        klass = self.make_class(teacher, "Class history")
        self.create_playground(teacher, "pg-history")
        first = self.publish(teacher, "pg-history", b"print(1)")
        second = self.publish(teacher, "pg-history", b"print(2)")
        self.assertNotEqual(first.commit_id, second.commit_id)

        status, body = self.create_release(
            teacher, klass.id, "pg-history", first.commit_id, "key-old"
        )

        self.assertEqual(status, 201)
        self.assertEqual(
            body["release"]["playgroundCommitId"], first.commit_id
        )
        self.assertEqual(
            self.release_rows()[0]["playground_commit_id"], first.commit_id
        )
        # The ref did move on, proving no substitution of current HEAD.
        self.assertEqual(self.ref_head("pg-history"), second.commit_id)

    def test_only_owning_teacher_may_create(self):
        teacher = self.create_user("teacher")
        klass = self.make_class(teacher, "Class owned")
        self.create_playground(teacher, "pg-owner")
        commit_id = self.publish(teacher, "pg-owner").commit_id

        student = self.create_user("student")
        other_teacher = self.create_user("teacher")

        for actor in (student, self.admin_actor, other_teacher, None, "user"):
            with self.subTest(actor=actor):
                with self.assertRaises(accounts.ForbiddenActionError):
                    self.create_release(
                        actor, klass.id, "pg-owner", commit_id, "key-denied"
                    )
        self.assertEqual(self.release_rows(), [])

    def test_missing_class_is_reported(self):
        teacher = self.create_user("teacher")
        self.create_playground(teacher, "pg-noclass")
        commit_id = self.publish(teacher, "pg-noclass").commit_id

        with self.assertRaises(classes.MissingClassError):
            self.create_release(
                teacher, "missing-class", "pg-noclass", commit_id,
                "key-missing",
            )

    def test_any_teacher_may_release_any_unarchived_playground(self):
        self.create_playground(None, "pg-auth")
        commit_id = self.publish(None, "pg-auth").commit_id

        teacher = self.create_user("teacher")
        klass = self.make_class(teacher, "Class other")

        status, body = self.create_release(
            teacher, klass.id, "pg-auth", commit_id, "key-release"
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["release"]["playgroundCommitId"], commit_id)

        # A second, unrelated Teacher may release the same Playground.
        second = self.create_user("teacher")
        second_class = self.make_class(second, "Class second")
        status, _ = self.create_release(
            second, second_class.id, "pg-auth", commit_id, "key-second"
        )
        self.assertEqual(status, 201)

        # Archived playgrounds are not releasable, even to an owned Class.
        playgrounds.update_playground(
            self.connection, self.admin_actor, "pg-auth", {"archived": True}
        )
        with self.assertRaises(accounts.ForbiddenActionError):
            self.create_release(
                teacher, klass.id, "pg-auth", commit_id, "key-archived"
            )

    def test_missing_playground_is_reported(self):
        teacher, klass, _, commit_id = self.teacher_class_playground("pg-real")

        with self.assertRaises(playgrounds.MissingPlaygroundError):
            self.create_release(
                teacher, klass.id, "pg-missing", commit_id, "key-nopg"
            )

    def test_unknown_and_foreign_commits_are_rejected(self):
        teacher = self.create_user("teacher")
        klass = self.make_class(teacher, "Class commits")
        self.create_playground(teacher, "pg-one")
        self.create_playground(teacher, "pg-two")
        other_commit = self.publish(teacher, "pg-two").commit_id

        with self.assertRaises(releases.UnknownPlaygroundCommitError):
            self.create_release(
                teacher, klass.id, "pg-one", other_commit, "key-foreign"
            )
        with self.assertRaises(releases.UnknownPlaygroundCommitError):
            self.create_release(
                teacher, klass.id, "pg-one", "f" * 40, "key-absent"
            )
        for malformed in ("", "abc", "-" + "a" * 39, "A" * 40, None):
            with self.subTest(malformed=malformed):
                with self.assertRaises(releases.InvalidReleaseRequestError):
                    self.create_release(
                        teacher, klass.id, "pg-one", malformed, "key-bad"
                    )
        self.assertEqual(self.release_rows(), [])

    def test_timestamp_and_boolean_validation(self):
        teacher, klass, playground_id, commit_id = (
            self.teacher_class_playground("pg-time")
        )

        for open_at in (
            datetime(2026, 1, 1),
            "2026-01-01T00:00:00",
            "not-a-time",
            None,
            7,
        ):
            with self.subTest(open_at=open_at):
                with self.assertRaises(releases.InvalidReleaseRequestError):
                    self.create_release(
                        teacher, klass.id, playground_id, commit_id,
                        "key-time", open_at=open_at,
                    )

        with self.assertRaises(releases.InvalidReleaseRequestError):
            self.create_release(
                teacher, klass.id, playground_id, commit_id, "key-due",
                open_at=PAST, due_at=PAST - timedelta(seconds=1),
            )

        with self.assertRaises(releases.InvalidReleaseRequestError):
            self.create_release(
                teacher, klass.id, playground_id, commit_id, "key-bool",
                requires_submission=1,
            )

        status, body = self.create_release(
            teacher, klass.id, playground_id, commit_id, "key-equal",
            open_at=PAST, due_at=PAST,
        )
        self.assertEqual(status, 201)
        self.assertEqual(
            body["release"]["dueAt"], body["release"]["openAt"]
        )
        self.assertEqual(len(self.release_rows()), 1)

    def test_exact_retry_replays_stored_result_without_duplicate(self):
        teacher, klass, playground_id, commit_id = (
            self.teacher_class_playground("pg-retry")
        )

        first = self.create_release(
            teacher, klass.id, playground_id, commit_id, "key-retry",
            open_at="2026-05-01T00:00:00Z",
        )
        # An equivalent timestamp representation is the same logical request.
        second = self.create_release(
            teacher, klass.id, playground_id, commit_id, "key-retry",
            open_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(first, second)
        self.assertEqual(first[0], 201)
        self.assertEqual(len(self.release_rows()), 1)

    def test_changed_body_with_same_key_conflicts(self):
        teacher = self.create_user("teacher")
        klass = self.make_class(teacher, "Class conflict")
        self.create_playground(teacher, "pg-conflict")
        first = self.publish(teacher, "pg-conflict", b"print(1)")
        second = self.publish(teacher, "pg-conflict", b"print(2)")

        self.create_release(
            teacher, klass.id, "pg-conflict", first.commit_id, "key-conflict"
        )
        with self.assertRaises(idempotency.IdempotencyConflict):
            self.create_release(
                teacher, klass.id, "pg-conflict", second.commit_id,
                "key-conflict",
            )
        self.assertEqual(len(self.release_rows()), 1)

    def test_release_is_frozen_and_has_no_mutation_api(self):
        release = releases.Release(
            id="r",
            class_id="c",
            playground_id="p",
            playground_commit_id="a" * 40,
            created_by="u",
            open_at=PAST.isoformat(),
            due_at=None,
            requires_submission=False,
            created_at=PAST.isoformat(),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            release.requires_submission = True

        self.assertFalse(hasattr(releases, "update_release"))
        self.assertFalse(hasattr(releases, "delete_release"))
        self.assertFalse(hasattr(releases, "patch_release"))


class ReleaseReadTest(ReleaseTestBase):
    def open_release(self, key="key-read", open_at=PAST):
        teacher = self.create_user("teacher")
        klass = self.make_class(teacher, "Class read")
        self.create_playground(teacher, "pg-read")
        commit_id = self.publish(teacher, "pg-read").commit_id
        _, body = self.create_release(
            teacher, klass.id, "pg-read", commit_id, key, open_at=open_at
        )
        return teacher, klass, body["release"]["id"]

    def test_student_sees_only_open_current_class_releases(self):
        teacher, klass, release_id = self.open_release(
            "key-future", open_at=FUTURE
        )
        student = self.create_user("student")
        self.assign_student(student, klass.id)

        self.assertEqual(
            releases.list_releases(self.connection, student, now=BEFORE), []
        )
        with self.assertRaises(releases.MissingReleaseError):
            releases.get_release(
                self.connection, student, release_id, now=BEFORE
            )

        # The owning teacher sees the future Release.
        self.assertEqual(
            [item.id for item in releases.list_releases(
                self.connection, teacher
            )],
            [release_id],
        )

        opened = releases.list_releases(self.connection, student, now=AFTER)
        self.assertEqual([item.id for item in opened], [release_id])
        self.assertEqual(
            releases.get_release(
                self.connection, student, release_id, now=AFTER
            ).id,
            release_id,
        )

    def test_transferred_student_loses_old_class_releases(self):
        teacher, klass, release_id = self.open_release("key-transfer")
        student = self.create_user("student")
        self.assign_student(student, klass.id)
        self.assertEqual(
            [item.id for item in releases.list_releases(
                self.connection, student
            )],
            [release_id],
        )

        other_teacher = self.create_user("teacher")
        other_class = self.make_class(other_teacher, "Class destination")
        self.assign_student(student, other_class.id)

        self.assertEqual(
            releases.list_releases(self.connection, student), []
        )
        with self.assertRaises(releases.MissingReleaseError):
            releases.get_release(self.connection, student, release_id)

    def test_teacher_lists_only_owned_class_releases(self):
        first_teacher, _, first_release = self.open_release("key-own-a")
        second_teacher = self.create_user("teacher")
        second_class = self.make_class(second_teacher, "Class second")
        self.create_playground(second_teacher, "pg-second")
        commit_id = self.publish(second_teacher, "pg-second").commit_id
        _, body = self.create_release(
            second_teacher, second_class.id, "pg-second", commit_id,
            "key-own-b",
        )

        self.assertEqual(
            [item.id for item in releases.list_releases(
                self.connection, first_teacher
            )],
            [first_release],
        )
        self.assertEqual(
            [item.id for item in releases.list_releases(
                self.connection, second_teacher
            )],
            [body["release"]["id"]],
        )

    def test_admin_lists_all_metadata_regardless_of_open(self):
        _, _, first_release = self.open_release("key-admin-a")
        teacher = self.create_user("teacher")
        klass = self.make_class(teacher, "Class future")
        self.create_playground(teacher, "pg-future")
        commit_id = self.publish(teacher, "pg-future").commit_id
        _, body = self.create_release(
            teacher, klass.id, "pg-future", commit_id, "key-admin-b",
            open_at=FUTURE,
        )

        listed = releases.list_releases(self.connection, self.admin_actor)
        self.assertEqual(
            sorted(item.id for item in listed),
            sorted([first_release, body["release"]["id"]]),
        )

    def test_get_hides_unauthorized_and_missing_releases(self):
        teacher, klass, release_id = self.open_release("key-hidden")
        other_teacher = self.create_user("teacher")
        outsider = self.create_user("student")
        missing_id = uuid.uuid4().hex

        with self.assertRaises(releases.MissingReleaseError):
            releases.get_release(self.connection, other_teacher, release_id)
        with self.assertRaises(releases.MissingReleaseError):
            releases.get_release(self.connection, outsider, release_id)
        with self.assertRaises(releases.MissingReleaseError):
            releases.get_release(self.connection, teacher, missing_id)
        self.assertEqual(
            releases.get_release(
                self.connection, self.admin_actor, release_id
            ).id,
            release_id,
        )

    def test_ordering_is_open_at_then_id(self):
        teacher = self.create_user("teacher")
        klass = self.make_class(teacher, "Class order")
        self.create_playground(teacher, "pg-order")
        commit_id = self.publish(teacher, "pg-order").commit_id

        earliest = None
        for key, open_at in (
            ("key-order-2", datetime(2026, 2, 1, tzinfo=timezone.utc)),
            ("key-order-1", datetime(2026, 1, 1, tzinfo=timezone.utc)),
            ("key-order-3", datetime(2026, 3, 1, tzinfo=timezone.utc)),
        ):
            _, body = self.create_release(
                teacher, klass.id, "pg-order", commit_id, key, open_at=open_at
            )
            if open_at == datetime(2026, 1, 1, tzinfo=timezone.utc):
                earliest = body["release"]["id"]

        listed = releases.list_releases(self.connection, self.admin_actor)
        self.assertEqual(listed[0].id, earliest)
        self.assertEqual(
            [item.open_at for item in listed],
            sorted(item.open_at for item in listed),
        )

        # The same open instant is broken deterministically by ID.
        _, body = self.create_release(
            teacher, klass.id, "pg-order", commit_id, "key-order-4",
            open_at=PAST,
        )
        _, other = self.create_release(
            teacher, klass.id, "pg-order", commit_id, "key-order-5",
            open_at=PAST,
        )
        same_open = [
            item for item in releases.list_releases(
                self.connection, self.admin_actor
            )
            if item.open_at == body["release"]["openAt"]
        ]
        self.assertEqual(
            [item.id for item in same_open],
            sorted(item.id for item in same_open),
        )
        self.assertIn(other["release"]["id"], [item.id for item in same_open])

    def test_invalid_actor_is_rejected_for_reads(self):
        _, _, release_id = self.open_release("key-invalid")
        for actor in (None, "actor", 7, object()):
            with self.subTest(actor=actor):
                with self.assertRaises(accounts.ForbiddenActionError):
                    releases.list_releases(self.connection, actor)
                with self.assertRaises(accounts.ForbiddenActionError):
                    releases.get_release(self.connection, actor, release_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
