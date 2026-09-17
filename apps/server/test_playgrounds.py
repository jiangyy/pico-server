"""Domain tests for playground metadata and Admin-only management."""

import dataclasses
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import accounts
import bundles
import playgrounds
import repository
from database import open_database


class PlaygroundDomainTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "app.db"
        self.connection = open_database(self.database_path)
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

    def tearDown(self):
        self.connection.close()
        self.temporary_directory.cleanup()

    def create_user(self, role):
        username = f"{role}-{uuid.uuid4().hex[:8]}"
        return accounts.create_user(
            self.connection,
            self.admin_actor,
            username,
            "secret",
            role,
        )

    def playground_policy(self, playground_id):
        row = self.connection.execute(
            "SELECT policy FROM playgrounds WHERE id = ?",
            (playground_id,),
        ).fetchone()
        return None if row is None else row["policy"]

    def test_create_requires_admin(self):
        actor = self.create_user("teacher")

        with self.assertRaises(accounts.ForbiddenActionError):
            playgrounds.create_playground(self.connection, actor, "pg-denied")
        self.assertIsNone(self.playground_policy("pg-denied"))

        playground = playgrounds.create_playground(
            self.connection, self.admin_actor, "pg-allowed"
        )
        self.assertEqual(playground.creator_id, self.admin_actor.id)
        self.assertEqual(playground.policy, "release_only")
        self.assertIsNone(playground.archived_at)
        self.assertTrue(playground.created_at)

    def test_admin_can_create_and_conflicts_are_rejected(self):
        playground = playgrounds.create_playground(
            self.connection, self.admin_actor, "pg-admin"
        )
        self.assertEqual(playground.creator_id, self.admin_actor.id)
        self.assertEqual(playground.policy, "release_only")

        with self.assertRaises(playgrounds.PlaygroundConflictError):
            playgrounds.create_playground(
                self.connection, self.admin_actor, "pg-admin"
            )

    def test_create_rejects_malformed_ids(self):
        for playground_id in (
            "",
            None,
            7,
            "../escape",
            "bad/id",
            "bad\\id",
            "bad id",
            "bad%2fid",
            ".leading",
            "trailing.",
            "double..dot",
            "name.lock",
            "NAME.LOCK",
            "caf\u00e9",
            "a" * 129,
        ):
            with self.subTest(playground_id=playground_id):
                with self.assertRaises(playgrounds.InvalidRequestError):
                    playgrounds.create_playground(
                        self.connection, self.admin_actor, playground_id
                    )

    def test_identifier_length_boundaries_and_subclass(self):
        for playground_id in ("a", "a" * 128, "A-z.0_9"):
            with self.subTest(playground_id=playground_id):
                created = playgrounds.create_playground(
                    self.connection, self.admin_actor, playground_id
                )
                self.assertEqual(created.id, playground_id)

        with self.assertRaises(playgrounds.InvalidRequestError):
            playgrounds.create_playground(
                self.connection, self.admin_actor, "a" * 129
            )

        class Sneaky(str):
            pass

        with self.assertRaises(playgrounds.InvalidRequestError):
            playgrounds.create_playground(
                self.connection, self.admin_actor, Sneaky("pg-subclass")
            )

    def test_policy_change_requires_admin(self):
        playgrounds.create_playground(
            self.connection, self.admin_actor, "pg-policy"
        )
        teacher = self.create_user("teacher")

        with self.assertRaises(accounts.ForbiddenActionError):
            playgrounds.update_playground(
                self.connection, teacher, "pg-policy", {"policy": "public"}
            )
        self.assertEqual(self.playground_policy("pg-policy"), "release_only")

        changed = playgrounds.update_playground(
            self.connection, self.admin_actor, "pg-policy",
            {"policy": "public"},
        )
        self.assertEqual(changed.policy, "public")

    def test_archive_and_unarchive(self):
        playgrounds.create_playground(
            self.connection, self.admin_actor, "pg-archive"
        )

        archived = playgrounds.update_playground(
            self.connection, self.admin_actor, "pg-archive", {"archived": True}
        )
        self.assertIsNotNone(archived.archived_at)

        retried = playgrounds.update_playground(
            self.connection, self.admin_actor, "pg-archive", {"archived": True}
        )
        self.assertEqual(retried.archived_at, archived.archived_at)

        restored = playgrounds.update_playground(
            self.connection, self.admin_actor, "pg-archive", {"archived": False}
        )
        self.assertIsNone(restored.archived_at)

        replay = playgrounds.update_playground(
            self.connection, self.admin_actor, "pg-archive", {"archived": False}
        )
        self.assertIsNone(replay.archived_at)

    def test_archive_requires_admin(self):
        teacher = self.create_user("teacher")
        playgrounds.create_playground(
            self.connection, self.admin_actor, "pg-archive"
        )

        with self.assertRaises(accounts.ForbiddenActionError):
            playgrounds.update_playground(
                self.connection, teacher, "pg-archive", {"archived": True}
            )
        self.assertIsNone(
            self.connection.execute(
                "SELECT archived_at FROM playgrounds WHERE id = ?",
                ("pg-archive",),
            ).fetchone()["archived_at"]
        )

    def test_update_missing_playground(self):
        for mutation in ({"policy": "public"}, {"archived": True}):
            with self.assertRaises(playgrounds.MissingPlaygroundError):
                playgrounds.update_playground(
                    self.connection, self.admin_actor, "missing", mutation
                )

    def test_update_rejects_invalid_mutations(self):
        playgrounds.create_playground(
            self.connection, self.admin_actor, "pg-invalid"
        )

        invalid = (
            {},
            {"policy": "public", "archived": True},
            {"policy": "secret"},
            {"policy": 7},
            {"policy": None},
            {"archived": "true"},
            {"archived": 1},
            {"archived": None},
            {"unknown": True},
            {"policy": "public", "extra": 1},
        )
        for mutation in invalid:
            with self.assertRaises(
                (playgrounds.InvalidRequestError, playgrounds.InvalidPolicyError)
            ):
                playgrounds.update_playground(
                    self.connection, self.admin_actor, "pg-invalid", mutation
                )

        with self.assertRaises(playgrounds.InvalidRequestError):
            playgrounds.update_playground(
                self.connection, self.admin_actor, "pg-invalid", []
            )
        self.assertEqual(self.playground_policy("pg-invalid"), "release_only")

    def test_invalid_actor_is_rejected_for_create(self):
        for actor in (None, "seed-actor", 7, object()):
            with self.assertRaises(accounts.ForbiddenActionError):
                playgrounds.create_playground(
                    self.connection, actor, "pg-invalid-actor"
                )
        self.assertIsNone(self.playground_policy("pg-invalid-actor"))

    def test_invalid_actor_is_rejected_for_policy_archive_and_unarchive(self):
        playgrounds.create_playground(
            self.connection, self.admin_actor, "pg-invalid-actor"
        )

        for actor in (None, "seed-actor", 7, object()):
            for mutation in (
                {"policy": "public"},
                {"archived": True},
                {"archived": False},
            ):
                with self.assertRaises(accounts.ForbiddenActionError):
                    playgrounds.update_playground(
                        self.connection, actor, "pg-invalid-actor", mutation
                    )

        row = self.connection.execute(
            "SELECT policy, archived_at FROM playgrounds WHERE id = ?",
            ("pg-invalid-actor",),
        ).fetchone()
        self.assertEqual(row["policy"], "release_only")
        self.assertIsNone(row["archived_at"])

    def test_list_playgrounds_is_bounded_ordered_and_admin_only(self):
        for playground_id in ("pg-a", "pg-b", "pg-c"):
            playgrounds.create_playground(
                self.connection, self.admin_actor, playground_id
            )
        first = playgrounds.list_playgrounds(
            self.connection, self.admin_actor, limit=2
        )
        self.assertEqual([item.id for item in first.items], ["pg-a", "pg-b"])
        self.assertEqual(first.next_key, "pg-b")
        rest = playgrounds.list_playgrounds(
            self.connection,
            self.admin_actor,
            limit=2,
            after=first.next_key,
        )
        self.assertEqual([item.id for item in rest.items], ["pg-c"])
        self.assertIsNone(rest.next_key)

        student = self.create_user("student")
        with self.assertRaises(accounts.ForbiddenActionError):
            playgrounds.list_playgrounds(
                self.connection, student, limit=2
            )

    def test_list_playgrounds_fails_closed_on_invalid_stored_id(self):
        with self.connection:
            self.connection.execute(
                "INSERT INTO playgrounds"
                " (id, creator_id, policy, created_at, archived_at)"
                " VALUES (?, ?, 'release_only', ?, NULL)",
                ("../bad", self.admin_actor.id, "2026-01-01T00:00:00Z"),
            )
        with self.assertRaises(playgrounds.StoredPlaygroundError):
            playgrounds.list_playgrounds(
                self.connection, self.admin_actor, limit=10
            )

    def test_releasable_playgrounds_are_ordered_unarchived_and_role_bounded(self):
        teacher = self.create_user("teacher")
        student = self.create_user("student")
        for playground_id in ("resource-c", "resource-a", "resource-b"):
            playgrounds.create_playground(
                self.connection, self.admin_actor, playground_id
            )
        playgrounds.update_playground(
            self.connection,
            self.admin_actor,
            "resource-b",
            {"archived": True},
        )

        page = playgrounds.list_releasable_playground_ids(
            self.connection, teacher, limit=10
        )
        self.assertEqual(page.items, ("resource-a", "resource-c"))
        self.assertIsNone(page.next_key)
        with self.assertRaises(accounts.ForbiddenActionError):
            playgrounds.list_releasable_playground_ids(
                self.connection, student, limit=10
            )


class PlaygroundPublishingTest(unittest.TestCase):
    """Authorization-order and end-to-end publishing tests."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "app.db"
        self.connection = open_database(self.database_path)
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
        self.path = Path(self.temporary_directory.name) / "picopg.git"
        self.repository = repository.open_repository(self.path)

    def tearDown(self):
        self.connection.close()
        self.temporary_directory.cleanup()

    def create_user(self, role):
        username = f"{role}-{uuid.uuid4().hex[:8]}"
        return accounts.create_user(
            self.connection,
            self.admin_actor,
            username,
            "secret",
            role,
        )

    def metadata(self, message="publish playground"):
        return repository.CommitMetadata(
            name="Test Author",
            email="author@example.test",
            timestamp="2026-01-01T00:00:00+00:00",
            message=message,
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

    def create(self, playground_id):
        return playgrounds.create_playground(
            self.connection, self.admin_actor, playground_id
        )

    def test_publish_requires_admin_before_git(self):
        self.create("pg-owner")

        bundle = self.make_bundle()
        metadata = self.metadata()
        with mock.patch.object(
            playgrounds._repository, "publish_playground"
        ) as spy:
            for actor in (self.create_user("teacher"), None, "not-a-user", 7):
                with self.subTest(actor=actor):
                    with self.assertRaises(accounts.ForbiddenActionError):
                        playgrounds.publish_playground(
                            self.connection,
                            actor,
                            "pg-owner",
                            self.repository,
                            bundle,
                            metadata,
                        )
            spy.assert_not_called()

            playgrounds.publish_playground(
                self.connection,
                self.admin_actor,
                "pg-owner",
                self.repository,
                bundle,
                metadata,
            )
            self.assertEqual(spy.call_count, 1)

    def test_missing_and_archived_are_checked_before_git(self):
        bundle = self.make_bundle()
        metadata = self.metadata()

        with mock.patch.object(
            playgrounds._repository, "publish_playground"
        ) as spy:
            with self.assertRaises(playgrounds.MissingPlaygroundError):
                playgrounds.publish_playground(
                    self.connection,
                    self.admin_actor,
                    "pg-missing",
                    self.repository,
                    bundle,
                    metadata,
                )
            spy.assert_not_called()

        playgrounds.create_playground(
            self.connection, self.admin_actor, "pg-archived"
        )
        playgrounds.update_playground(
            self.connection,
            self.admin_actor,
            "pg-archived",
            {"archived": True},
        )

        with mock.patch.object(
            playgrounds._repository, "publish_playground"
        ) as spy:
            with self.assertRaises(accounts.ForbiddenActionError):
                playgrounds.publish_playground(
                    self.connection,
                    self.admin_actor,
                    "pg-archived",
                    self.repository,
                    bundle,
                    metadata,
                )
            spy.assert_not_called()

    def test_admin_publishes_real_commits(self):
        self.create("pg-real")
        metadata = self.metadata()

        first = playgrounds.publish_playground(
            self.connection,
            self.admin_actor,
            "pg-real",
            self.repository,
            self.make_bundle(b"print(1)"),
            metadata,
        )
        self.assertIsInstance(first, repository.PublishResult)
        self.assertEqual(first.playground_id, "pg-real")
        self.assertFalse(first.no_op)
        self.assertTrue(first.commit_id)

        # The admin publishing the identical tree is a no-op on the same commit.
        second = playgrounds.publish_playground(
            self.connection,
            self.admin_actor,
            "pg-real",
            self.repository,
            self.make_bundle(b"print(1)"),
            metadata,
        )
        self.assertTrue(second.no_op)
        self.assertEqual(second.commit_id, first.commit_id)

        ref = self.repository.playground_ref("pg-real")
        completed = subprocess.run(
            ["git", "-C", str(self.path), "rev-parse", ref],
            capture_output=True,
            check=True,
        )
        self.assertEqual(completed.stdout.strip().decode(), first.commit_id)

    def test_legacy_invalid_playground_row_is_fail_closed(self):
        # A row that predates the identifier contract must never be silently
        # deleted or rewritten; normal operations fail closed instead.
        playground_id = "../escape"
        with self.connection:
            self.connection.execute(
                "INSERT INTO playgrounds"
                " (id, creator_id, policy, created_at, archived_at)"
                " VALUES (?, ?, ?, ?, NULL)",
                (
                    playground_id,
                    self.admin_actor.id,
                    playgrounds.RELEASE_ONLY_POLICY,
                    "2026-01-01T00:00:00.000000Z",
                ),
            )

        with self.assertRaises(playgrounds.InvalidRequestError):
            playgrounds.update_playground(
                self.connection,
                self.admin_actor,
                playground_id,
                {"archived": True},
            )
        with self.assertRaises(playgrounds.InvalidRequestError):
            playgrounds.publish_playground(
                self.connection,
                self.admin_actor,
                playground_id,
                self.repository,
                self.make_bundle(),
                self.metadata(),
            )

    def test_publish_result_is_frozen(self):
        playgrounds.create_playground(
            self.connection, self.admin_actor, "pg-frozen"
        )
        result = playgrounds.publish_playground(
            self.connection,
            self.admin_actor,
            "pg-frozen",
            self.repository,
            self.make_bundle(),
            self.metadata(),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.no_op = True

    def test_domain_rejects_mutable_bundle(self):
        playgrounds.create_playground(
            self.connection, self.admin_actor, "pg-mutable"
        )
        with self.assertRaises(repository.RepositoryError):
            playgrounds.publish_playground(
                self.connection,
                self.admin_actor,
                "pg-mutable",
                self.repository,
                {
                    "picopg.json": b'{"schemaVersion":1,"runtime":"python"}'
                },
                self.metadata(),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
