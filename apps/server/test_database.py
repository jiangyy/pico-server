"""Tests for SQLite bootstrap."""

import sqlite3
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
        self.assertEqual(
            [row["version"] for row in versions],
            list(range(1, SCHEMA_VERSION + 1)),
        )

    def test_version_one_database_is_upgraded(self):
        self.database_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations (version, applied_at)
            VALUES (1, '2026-09-15T00:00:00Z');
            """
        )
        connection.close()

        upgraded = self.open()

        user_table = upgraded.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        self.assertIsNotNone(user_table)

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

    def test_user_role_is_constrained(self):
        connection = self.open()

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO users (id, username, role, password_hash, created_at)
                VALUES ('user-1', 'alice', 'publisher', 'hash', '2026-09-15T00:00:00Z')
                """
            )

    def test_username_is_unique(self):
        connection = self.open()
        rows = (
            ("user-1", "alice", "student", "hash-1", "2026-09-15T00:00:00Z"),
            ("user-2", "alice", "teacher", "hash-2", "2026-09-15T00:00:01Z"),
        )
        connection.execute(
            """
            INSERT INTO users (id, username, role, password_hash, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows[0],
        )

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO users (id, username, role, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows[1],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
