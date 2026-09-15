"""Tests for SQLite bootstrap."""

import tempfile
import unittest
from pathlib import Path

from database import SCHEMA_VERSION, open_database


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "nested" / "app.db"

    def open(self):
        connection = open_database(self.database_path)
        self.addCleanup(connection.close)
        return connection

    def test_open_creates_database_with_foreign_keys_enabled(self):
        connection = self.open()

        self.assertTrue(self.database_path.is_file())
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_schema_initialization_is_repeatable(self):
        self.open().close()
        connection = self.open()

        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual([row["version"] for row in versions], [SCHEMA_VERSION])

    def test_newer_schema_is_rejected(self):
        connection = self.open()
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION + 1, "2026-09-15T00:00:00Z"),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            open_database(self.database_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
