"""Tests for the SQLite bootstrap and schema validation.

The schema is one version describing three tables: the published Playgrounds,
the idempotency results, and the submissions.  Validation is exact, because a
database that is one column off would fail much later and much less clearly.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Allow ``import database`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import (  # noqa: E402
    SCHEMA_VERSION,
    SchemaValidationError,
    open_database,
    validate_schema,
)


class OpenDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "data" / "app.db"

    def test_opening_creates_the_file_and_the_schema(self):
        connection = open_database(self.path)
        self.addCleanup(connection.close)

        self.assertTrue(self.path.is_file())
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertLessEqual(
            {"playgrounds", "idempotency_results", "submissions"}, tables
        )

    def test_opening_is_repeatable(self):
        first = open_database(self.path)
        first.close()
        second = open_database(self.path)
        self.addCleanup(second.close)

        versions = [
            row["version"]
            for row in second.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        self.assertEqual(versions, [1, SCHEMA_VERSION])

    def test_foreign_keys_are_enforced(self):
        connection = open_database(self.path)
        self.addCleanup(connection.close)

        with self.assertRaises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "INSERT INTO submissions (id, playground_id,"
                    " playground_commit_id, student, files_json, created_at)"
                    " VALUES ('s', 'pg-missing', ?, 'a', '{}', 'now')",
                    ("0" * 40,),
                )

    def test_a_submission_may_not_be_named_twice(self):
        connection = open_database(self.path)
        self.addCleanup(connection.close)
        with connection:
            connection.execute(
                "INSERT INTO playgrounds (id, created_at) VALUES ('pg', 'now')"
            )
            connection.execute(
                "INSERT INTO submissions (id, playground_id,"
                " playground_commit_id, student, files_json, created_at)"
                " VALUES ('s', 'pg', ?, 'a', '{}', 'now')",
                ("0" * 40,),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "INSERT INTO submissions (id, playground_id,"
                    " playground_commit_id, student, files_json, created_at)"
                    " VALUES ('s', 'pg', ?, 'b', '{}', 'now')",
                    ("0" * 40,),
                )

    def test_the_database_enforces_the_student_length(self):
        connection = open_database(self.path)
        self.addCleanup(connection.close)
        with connection:
            connection.execute(
                "INSERT INTO playgrounds (id, created_at) VALUES ('pg', 'now')"
            )

        for student in ("", "a" * 129):
            with self.subTest(student=student):
                with self.assertRaises(sqlite3.IntegrityError):
                    with connection:
                        connection.execute(
                            "INSERT INTO submissions (id, playground_id,"
                            " playground_commit_id, student, files_json,"
                            " created_at) VALUES (?, 'pg', ?, ?, '{}', 'now')",
                            ("s", "0" * 40, student),
                        )


class ValidateSchemaTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "app.db"
        connection = open_database(self.path)
        connection.close()
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(self.connection.close)

    def test_the_current_schema_validates(self):
        validate_schema(self.connection)

    def test_a_missing_table_is_reported(self):
        with self.connection:
            self.connection.execute("DROP TABLE submissions")
        with self.assertRaises(SchemaValidationError):
            validate_schema(self.connection)

    def test_a_missing_column_is_reported(self):
        with self.connection:
            self.connection.execute("ALTER TABLE playgrounds RENAME TO old")
            self.connection.execute(
                "CREATE TABLE playgrounds (id TEXT PRIMARY KEY)"
            )
        with self.assertRaises(SchemaValidationError):
            validate_schema(self.connection)

    def test_a_future_migration_version_is_reported(self):
        with self.connection:
            self.connection.execute(
                "INSERT INTO schema_migrations (version, applied_at)"
                " VALUES (?, 'now')",
                (SCHEMA_VERSION + 1,),
            )
        with self.assertRaises(SchemaValidationError):
            validate_schema(self.connection)

    def test_a_gapped_migration_history_is_reported(self):
        with self.connection:
            self.connection.execute("DELETE FROM schema_migrations")
        with self.assertRaises(SchemaValidationError):
            validate_schema(self.connection)

    def test_a_missing_index_is_reported(self):
        with self.connection:
            self.connection.execute("DROP INDEX submissions_playground_idx")
        with self.assertRaises(SchemaValidationError):
            validate_schema(self.connection)

    def test_a_missing_foreign_key_is_reported(self):
        with self.connection:
            self.connection.execute("PRAGMA foreign_keys = OFF")
            self.connection.execute("DROP TABLE submissions")
            self.connection.execute(
                "CREATE TABLE submissions ("
                " id TEXT PRIMARY KEY NOT NULL,"
                " playground_id TEXT NOT NULL,"
                " playground_commit_id TEXT NOT NULL,"
                " student TEXT NOT NULL,"
                " files_json TEXT NOT NULL,"
                " created_at TEXT NOT NULL)"
            )
            self.connection.execute(
                "CREATE INDEX submissions_playground_idx"
                " ON submissions(playground_id, created_at)"
            )
        with self.assertRaises(SchemaValidationError):
            validate_schema(self.connection)

    def test_a_future_version_refuses_to_migrate(self):
        self.connection.close()
        connection = sqlite3.connect(self.path)
        with connection:
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at)"
                " VALUES (?, 'now')",
                (SCHEMA_VERSION + 1,),
            )
        connection.close()

        with self.assertRaises(SchemaValidationError):
            open_database(self.path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
