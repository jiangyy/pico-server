"""Tests for published Playground metadata.

A Playground row only records that a bundle was published under an opaque
identifier; the identifiers are the interesting part, because one reaches SQL
and a Git ref.  These tests run against a real SQLite database and a real bare
repository in a temporary directory.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Allow ``import playgrounds`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bundles  # noqa: E402
import playgrounds  # noqa: E402
import repository  # noqa: E402
import manifests  # noqa: E402


def repository_module_head(repo, playground_id):
    return manifests.current_playground_commit(repo, playground_id)
from database import open_database  # noqa: E402

MANIFEST_BYTES = b'{"schemaVersion":1,"runtime":"python"}'


class PlaygroundTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        base = Path(self.temporary_directory.name)
        self.connection = open_database(base / "app.db")
        self.addCleanup(self.connection.close)
        self.repository = repository.open_repository(base / "picopg.git")

    def bundle(self, body=b"print(1)"):
        return bundles.validate_bundle(
            [
                bundles.BundleEntry("picopg.json", MANIFEST_BYTES),
                bundles.BundleEntry("course/module/app.py", body),
            ]
        )

    def metadata(self):
        return repository.CommitMetadata(
            name="PicoPG Operator",
            email="operator@picopg.invalid",
            timestamp="2026-01-01T00:00:00.000000Z",
            message="Publish playground bundle",
        )

    # ----- identifiers ---------------------------------------------------

    def test_identifiers_are_validated_before_storage(self):
        for identifier in ("", "bad/id", "bad id", "a" * 129, None, 7):
            with self.subTest(identifier=identifier):
                with self.assertRaises(playgrounds.InvalidRequestError):
                    playgrounds.create_playground(self.connection, identifier)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) AS n FROM playgrounds"
            ).fetchone()["n"],
            0,
        )

    def test_creating_the_same_identifier_twice_conflicts(self):
        playgrounds.create_playground(self.connection, "pg-one")

        with self.assertRaises(playgrounds.PlaygroundConflictError):
            playgrounds.create_playground(self.connection, "pg-one")

    def test_a_created_playground_can_be_read_back(self):
        created = playgrounds.create_playground(self.connection, "pg-one")

        stored = playgrounds.get_playground(self.connection, "pg-one")
        self.assertEqual(stored, created)
        self.assertEqual(created.id, "pg-one")
        self.assertTrue(created.created_at)

    def test_an_unknown_playground_reads_as_none(self):
        self.assertIsNone(playgrounds.get_playground(self.connection, "pg-none"))

    # ----- publishing ----------------------------------------------------

    def test_publishing_an_unknown_playground_leaves_git_untouched(self):
        with self.assertRaises(playgrounds.MissingPlaygroundError):
            playgrounds.publish_playground(
                self.connection,
                "pg-none",
                self.repository,
                self.bundle(),
                self.metadata(),
            )

        self.assertFalse(
            (self.repository.path / "refs" / "heads" / "playgrounds").exists()
        )

    def test_publishing_records_a_commit_and_a_no_op_republish(self):
        playgrounds.create_playground(self.connection, "pg-one")

        first = playgrounds.publish_playground(
            self.connection, "pg-one", self.repository, self.bundle(), self.metadata()
        )
        self.assertFalse(first.no_op)
        self.assertEqual(len(first.commit_id), 40)

        again = playgrounds.publish_playground(
            self.connection, "pg-one", self.repository, self.bundle(), self.metadata(),
            base_version_id=first.commit_id,
        )
        self.assertTrue(again.no_op)
        self.assertEqual(again.commit_id, first.commit_id)

    def test_a_new_bundle_appends_a_new_commit(self):
        playgrounds.create_playground(self.connection, "pg-one")

        first = playgrounds.publish_playground(
            self.connection, "pg-one", self.repository, self.bundle(b"print(1)"),
            self.metadata(),
        )
        second = playgrounds.publish_playground(
            self.connection, "pg-one", self.repository, self.bundle(b"print(2)"),
            self.metadata(),
            base_version_id=first.commit_id,
        )
        self.assertNotEqual(first.commit_id, second.commit_id)
        self.assertTrue(
            repository.commit_in_playground_history(
                self.repository, "pg-one", first.commit_id
            )
        )


class BackfillCurrentVersionsTest(PlaygroundTest):
    """`current_version_id` is what the catalog reads, so it must be complete.

    A row from before that column existed has no value; start-up fills it in
    from the ref once, instead of the catalog running a Git subprocess per row
    per request. A row that never stored one is *not* in the catalog, so this is
    the step that keeps an upgraded deployment from losing its courses.
    """

    def stored(self, playground_id):
        row = self.connection.execute(
            "SELECT current_version_id FROM playgrounds WHERE id = ?",
            (playground_id,),
        ).fetchone()
        return None if row is None else row["current_version_id"]

    def blank(self, playground_id):
        """Erase the column, as a v1 → v2 migration leaves it."""

        with self.connection:
            self.connection.execute(
                "UPDATE playgrounds SET current_version_id = '' WHERE id = ?",
                (playground_id,),
            )

    def unfilled(self, playground_id):
        """A registered row that predates the column."""

        playgrounds.create_playground(self.connection, playground_id)
        self.blank(playground_id)

    def test_a_row_with_a_readable_ref_is_filled_in(self):
        playgrounds.create_playground(self.connection, "pg")
        result = playgrounds.publish_playground(
            self.connection, "pg", self.repository, self.bundle(), self.metadata()
        )
        self.blank("pg")
        self.assertEqual(self.stored("pg"), "")

        filled = playgrounds.backfill_current_versions(
            self.connection,
            lambda playground_id: repository_module_head(self.repository, playground_id),
        )
        self.assertEqual(filled, 1)
        self.assertEqual(self.stored("pg"), result.commit_id)

    def test_a_row_with_no_ref_is_left_alone(self):
        self.unfilled("pg-never-published")
        filled = playgrounds.backfill_current_versions(
            self.connection, lambda playground_id: None
        )
        self.assertEqual(filled, 0)
        self.assertEqual(self.stored("pg-never-published"), "")

    def test_a_row_that_already_has_a_version_is_not_resolved_again(self):
        playgrounds.create_playground(self.connection, "pg")
        playgrounds.publish_playground(
            self.connection, "pg", self.repository, self.bundle(), self.metadata()
        )
        asked = []
        filled = playgrounds.backfill_current_versions(
            self.connection, lambda playground_id: asked.append(playground_id)
        )
        self.assertEqual(filled, 0)
        self.assertEqual(asked, [], "an already-filled row must cost nothing")

    def test_an_archived_row_is_not_resolved(self):
        self.unfilled("pg-archived")
        with self.connection:
            self.connection.execute(
                "UPDATE playgrounds SET state = 'stopped' WHERE id = ?",
                ("pg-archived",),
            )
        asked = []
        playgrounds.backfill_current_versions(
            self.connection, lambda playground_id: asked.append(playground_id)
        )
        self.assertEqual(asked, [], "a stopped row is not in the catalog anyway")

    def test_it_is_idempotent(self):
        self.unfilled("pg")
        resolve = lambda playground_id: "a" * 40
        self.assertEqual(playgrounds.backfill_current_versions(self.connection, resolve), 1)
        self.assertEqual(playgrounds.backfill_current_versions(self.connection, resolve), 0)
        self.assertEqual(self.stored("pg"), "a" * 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
