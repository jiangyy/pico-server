"""Domain tests for playground metadata and publisher authorization."""

import tempfile
import unittest
import uuid
from pathlib import Path

import accounts
import playgrounds
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

    def grant_count(self, user_id):
        rows = self.connection.execute(
            "SELECT user_id FROM publisher_grants WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return len(rows)

    def playground_policy(self, playground_id):
        row = self.connection.execute(
            "SELECT policy FROM playgrounds WHERE id = ?",
            (playground_id,),
        ).fetchone()
        return None if row is None else row["policy"]

    def test_grant_is_idempotent_and_records_grant(self):
        user = self.create_user("teacher")

        first = playgrounds.grant_publisher(
            self.connection, self.admin_actor, user.id
        )
        self.assertEqual(first.user_id, user.id)
        self.assertEqual(first.granted_by, self.admin_actor.id)
        self.assertTrue(first.created_at)

        retry = playgrounds.grant_publisher(
            self.connection, self.admin_actor, user.id
        )
        self.assertEqual(retry, first)
        self.assertEqual(self.grant_count(user.id), 1)

    def test_revoke_is_idempotent(self):
        user = self.create_user("teacher")
        never_granted = self.create_user("teacher")

        playgrounds.revoke_publisher(
            self.connection, self.admin_actor, never_granted.id
        )
        self.assertEqual(self.grant_count(never_granted.id), 0)

        playgrounds.grant_publisher(self.connection, self.admin_actor, user.id)
        playgrounds.revoke_publisher(self.connection, self.admin_actor, user.id)
        playgrounds.revoke_publisher(self.connection, self.admin_actor, user.id)
        self.assertEqual(self.grant_count(user.id), 0)

    def test_grant_and_revoke_require_admin(self):
        actor = self.create_user("teacher")
        target = self.create_user("student")

        with self.assertRaises(accounts.ForbiddenActionError):
            playgrounds.grant_publisher(self.connection, actor, target.id)
        with self.assertRaises(accounts.ForbiddenActionError):
            playgrounds.revoke_publisher(self.connection, actor, target.id)
        self.assertEqual(self.grant_count(target.id), 0)

    def test_grant_and_revoke_require_existing_user(self):
        with self.assertRaises(accounts.MissingUserError):
            playgrounds.grant_publisher(
                self.connection, self.admin_actor, "missing-user"
            )
        with self.assertRaises(accounts.MissingUserError):
            playgrounds.revoke_publisher(
                self.connection, self.admin_actor, "missing-user"
            )
        with self.assertRaises(playgrounds.InvalidRequestError):
            playgrounds.grant_publisher(self.connection, self.admin_actor, "")
        with self.assertRaises(playgrounds.InvalidRequestError):
            playgrounds.revoke_publisher(self.connection, self.admin_actor, None)

    def test_create_requires_admin_or_grant(self):
        actor = self.create_user("teacher")

        with self.assertRaises(accounts.ForbiddenActionError):
            playgrounds.create_playground(self.connection, actor, "pg-denied")
        self.assertIsNone(self.playground_policy("pg-denied"))

        playgrounds.grant_publisher(self.connection, self.admin_actor, actor.id)
        playground = playgrounds.create_playground(
            self.connection, actor, "pg-allowed"
        )
        self.assertEqual(playground.creator_id, actor.id)
        self.assertEqual(playground.policy, "release_only")
        self.assertIsNone(playground.archived_at)
        self.assertTrue(playground.created_at)

    def test_revoked_user_cannot_create(self):
        actor = self.create_user("teacher")
        playgrounds.grant_publisher(self.connection, self.admin_actor, actor.id)
        playgrounds.revoke_publisher(self.connection, self.admin_actor, actor.id)

        with self.assertRaises(accounts.ForbiddenActionError):
            playgrounds.create_playground(self.connection, actor, "pg-revoked")
        self.assertIsNone(self.playground_policy("pg-revoked"))

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
        for playground_id in ("", None, 7):
            with self.assertRaises(playgrounds.InvalidRequestError):
                playgrounds.create_playground(
                    self.connection, self.admin_actor, playground_id
                )

    def test_policy_change_by_admin_and_creator(self):
        creator = self.create_user("teacher")
        playgrounds.grant_publisher(
            self.connection, self.admin_actor, creator.id
        )
        playgrounds.create_playground(self.connection, creator, "pg-policy")

        changed = playgrounds.update_playground(
            self.connection, creator, "pg-policy", {"policy": "public"}
        )
        self.assertEqual(changed.policy, "public")

        changed = playgrounds.update_playground(
            self.connection, self.admin_actor, "pg-policy",
            {"policy": "release_only"},
        )
        self.assertEqual(changed.policy, "release_only")

    def test_policy_change_rejects_unrelated_actor(self):
        creator = self.create_user("teacher")
        other = self.create_user("teacher")
        playgrounds.grant_publisher(
            self.connection, self.admin_actor, creator.id
        )
        playgrounds.grant_publisher(self.connection, self.admin_actor, other.id)
        playgrounds.create_playground(self.connection, creator, "pg-policy")

        with self.assertRaises(accounts.ForbiddenActionError):
            playgrounds.update_playground(
                self.connection, other, "pg-policy", {"policy": "public"}
            )
        self.assertEqual(self.playground_policy("pg-policy"), "release_only")

    def test_archive_and_unarchive(self):
        creator = self.create_user("teacher")
        playgrounds.grant_publisher(
            self.connection, self.admin_actor, creator.id
        )
        playgrounds.create_playground(self.connection, creator, "pg-archive")

        archived = playgrounds.update_playground(
            self.connection, creator, "pg-archive", {"archived": True}
        )
        self.assertIsNotNone(archived.archived_at)

        retried = playgrounds.update_playground(
            self.connection, creator, "pg-archive", {"archived": True}
        )
        self.assertEqual(retried.archived_at, archived.archived_at)

        with self.assertRaises(accounts.ForbiddenActionError):
            playgrounds.update_playground(
                self.connection, creator, "pg-archive", {"archived": False}
            )

        restored = playgrounds.update_playground(
            self.connection, self.admin_actor, "pg-archive", {"archived": False}
        )
        self.assertIsNone(restored.archived_at)

        replay = playgrounds.update_playground(
            self.connection, self.admin_actor, "pg-archive", {"archived": False}
        )
        self.assertIsNone(replay.archived_at)

    def test_archive_requires_creator_or_admin(self):
        creator = self.create_user("teacher")
        other = self.create_user("teacher")
        playgrounds.grant_publisher(
            self.connection, self.admin_actor, creator.id
        )
        playgrounds.grant_publisher(self.connection, self.admin_actor, other.id)
        playgrounds.create_playground(self.connection, creator, "pg-archive")

        with self.assertRaises(accounts.ForbiddenActionError):
            playgrounds.update_playground(
                self.connection, other, "pg-archive", {"archived": True}
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

    def test_invalid_actor_is_rejected_for_grant_and_revoke(self):
        target = self.create_user("student")
        playgrounds.grant_publisher(
            self.connection, self.admin_actor, target.id
        )

        for actor in (None, "seed-actor", target.id, 7, object()):
            with self.assertRaises(accounts.ForbiddenActionError):
                playgrounds.grant_publisher(self.connection, actor, target.id)
            with self.assertRaises(accounts.ForbiddenActionError):
                playgrounds.revoke_publisher(self.connection, actor, target.id)

        # The pre-existing grant survives and no extra grant was written.
        self.assertEqual(self.grant_count(target.id), 1)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
