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
        # The chain is walked in order, one record per step: re-opening an
        # already-current file must not add a second record for any step.
        self.assertEqual(versions, list(range(1, SCHEMA_VERSION + 1)))

    def test_a_submission_names_an_assignment_and_a_version(self):
        # 提交对准的对象只有一种（Assignment）。"这道作业存在"由应用层检查——数据库
        # 这一层守形状：两个 id 都不能是空串。
        connection = open_database(self.path)
        self.addCleanup(connection.close)

        for column in ("assignment_id", "version_id"):
            with self.subTest(column=column):
                values = {"assignment_id": "hw", "version_id": "0" * 40}
                values[column] = ""
                with self.assertRaises(sqlite3.IntegrityError):
                    with connection:
                        connection.execute(
                            "INSERT INTO submissions (id, assignment_id,"
                            " version_id, student, files_json, created_at)"
                            " VALUES ('s', ?, ?, 'a', '{}', 'now')",
                            (values["assignment_id"], values["version_id"]),
                        )

    def test_a_submission_may_not_be_named_twice(self):
        connection = open_database(self.path)
        self.addCleanup(connection.close)
        with connection:
            connection.execute(
                "INSERT INTO submissions (id, assignment_id,"
                " version_id, student, files_json, created_at)"
                " VALUES ('s', 'hw', ?, 'a', '{}', 'now')",
                ("0" * 40,),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "INSERT INTO submissions (id, assignment_id,"
                    " version_id, student, files_json, created_at)"
                    " VALUES ('s', 'hw', ?, 'b', '{}', 'now')",
                    ("0" * 40,),
                )

    def test_the_database_enforces_the_student_length(self):
        connection = open_database(self.path)
        self.addCleanup(connection.close)

        for student in ("", "a" * 129):
            with self.subTest(student=student):
                with self.assertRaises(sqlite3.IntegrityError):
                    with connection:
                        connection.execute(
                            "INSERT INTO submissions (id, assignment_id,"
                            " version_id, student, files_json,"
                            " created_at)"
                            " VALUES (?, 'hw', ?, ?, '{}', 'now')",
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
            self.connection.execute("DROP INDEX submissions_assignment_idx")
        with self.assertRaises(SchemaValidationError):
            validate_schema(self.connection)

    def test_a_table_with_the_wrong_columns_is_reported(self):
        with self.connection:
            self.connection.execute("DROP TABLE submissions")
            self.connection.execute(
                "CREATE TABLE submissions ("
                " id TEXT PRIMARY KEY NOT NULL,"
                " playground_id TEXT NOT NULL,"
                " version_id TEXT NOT NULL,"
                " student TEXT NOT NULL,"
                " files_json TEXT NOT NULL,"
                " created_at TEXT NOT NULL)"
            )
            self.connection.execute(
                "CREATE INDEX submissions_assignment_idx"
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
