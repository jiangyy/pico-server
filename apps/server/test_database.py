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

    def insert_user(self, connection, user_id="user-1", username="alice"):
        connection.execute(
            """
            INSERT INTO users (id, username, role, password_hash, created_at)
            VALUES (?, ?, 'student', 'hash', '2026-09-15T00:00:00Z')
            """,
            (user_id, username),
        )

    def insert_class(
        self,
        connection,
        class_id="class-1",
        teacher_id="teacher-1",
        name="Algebra",
    ):
        connection.execute(
            """
            INSERT INTO classes (id, name, teacher_id, created_at)
            VALUES (?, ?, ?, '2026-09-15T00:00:00Z')
            """,
            (class_id, name, teacher_id),
        )

    def insert_membership(self, connection, student_id, class_id):
        connection.execute(
            """
            INSERT INTO class_memberships (student_id, class_id, joined_at)
            VALUES (?, ?, '2026-09-15T00:00:00Z')
            """,
            (student_id, class_id),
        )

    def insert_playground(
        self,
        connection,
        playground_id="playground-1",
        creator_id="teacher-1",
        policy="release_only",
    ):
        connection.execute(
            """
            INSERT INTO playgrounds (id, creator_id, policy, created_at)
            VALUES (?, ?, ?, '2026-09-15T00:00:00Z')
            """,
            (playground_id, creator_id, policy),
        )

    def insert_publisher_grant(
        self,
        connection,
        user_id="user-1",
        granted_by="admin-1",
    ):
        connection.execute(
            """
            INSERT INTO publisher_grants (user_id, granted_by, created_at)
            VALUES (?, ?, '2026-09-15T00:00:00Z')
            """,
            (user_id, granted_by),
        )

    def create_version_three_database(self):
        self.database_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations (version, applied_at) VALUES
                (1, '2026-09-15T00:00:00Z'),
                (2, '2026-09-15T00:00:01Z'),
                (3, '2026-09-15T00:00:02Z');
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL
                    CHECK (role IN ('student', 'teacher', 'admin')),
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            );
            CREATE INDEX sessions_user_id_idx ON sessions(user_id);
            INSERT INTO users (id, username, role, password_hash, created_at)
            VALUES ('user-1', 'alice', 'teacher', 'hash', '2026-09-15T00:00:00Z');
            INSERT INTO sessions (token_hash, user_id, created_at, expires_at)
            VALUES ('token-hash', 'user-1', '2026-09-15T00:00:00Z',
                    '2026-09-16T00:00:00Z');
            """
        )
        connection.close()

    def create_version_four_database(self):
        self.database_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations (version, applied_at) VALUES
                (1, '2026-09-15T00:00:00Z'),
                (2, '2026-09-15T00:00:01Z'),
                (3, '2026-09-15T00:00:02Z'),
                (4, '2026-09-15T00:00:03Z');
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL
                    CHECK (role IN ('student', 'teacher', 'admin')),
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            );
            CREATE INDEX sessions_user_id_idx ON sessions(user_id);
            CREATE TABLE classes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL CHECK (name <> ''),
                teacher_id TEXT NOT NULL
                    REFERENCES users(id) ON DELETE RESTRICT,
                created_at TEXT NOT NULL,
                archived_at TEXT
            );
            CREATE TABLE class_memberships (
                student_id TEXT PRIMARY KEY
                    REFERENCES users(id) ON DELETE RESTRICT,
                class_id TEXT NOT NULL
                    REFERENCES classes(id) ON DELETE RESTRICT,
                joined_at TEXT NOT NULL
            );
            CREATE INDEX classes_teacher_id_idx ON classes(teacher_id);
            CREATE INDEX class_memberships_class_id_idx
                ON class_memberships(class_id);
            INSERT INTO users (id, username, role, password_hash, created_at)
            VALUES
                ('teacher-1', 'teacher', 'teacher', 'hash-1',
                 '2026-09-15T00:00:00Z'),
                ('student-1', 'student', 'student', 'hash-2',
                 '2026-09-15T00:00:00Z');
            INSERT INTO sessions (token_hash, user_id, created_at, expires_at)
            VALUES ('token-hash', 'teacher-1', '2026-09-15T00:00:00Z',
                    '2026-09-16T00:00:00Z');
            INSERT INTO classes (id, name, teacher_id, created_at)
            VALUES ('class-1', 'Algebra', 'teacher-1',
                    '2026-09-15T00:00:00Z');
            INSERT INTO class_memberships (student_id, class_id, joined_at)
            VALUES ('student-1', 'class-1', '2026-09-15T00:00:00Z');
            """
        )
        connection.close()

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

        tables = upgraded.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name IN ('users', 'sessions')
            """
        ).fetchall()
        self.assertEqual({row["name"] for row in tables}, {"users", "sessions"})

    def test_class_schema_is_created(self):
        connection = self.open()

        tables = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name IN ('classes', 'class_memberships')
            """
        ).fetchall()
        self.assertEqual(
            {row["name"] for row in tables}, {"classes", "class_memberships"}
        )

        indexes = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name IN (
                'classes_teacher_id_idx', 'class_memberships_class_id_idx'
            )
            """
        ).fetchall()
        self.assertEqual(
            {row["name"] for row in indexes},
            {"classes_teacher_id_idx", "class_memberships_class_id_idx"},
        )

    def test_version_three_database_is_upgraded(self):
        self.create_version_three_database()

        connection = self.open()

        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual([row["version"] for row in versions], [1, 2, 3, 4, 5])
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 4"
            ).fetchone()[0],
            1,
        )
        users = connection.execute("SELECT id, username FROM users").fetchall()
        self.assertEqual(
            [(row["id"], row["username"]) for row in users], [("user-1", "alice")]
        )
        sessions = connection.execute("SELECT token_hash FROM sessions").fetchall()
        self.assertEqual([row["token_hash"] for row in sessions], ["token-hash"])

    def test_playground_policy_schema_is_created(self):
        connection = self.open()

        tables = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name IN ('playgrounds', 'publisher_grants')
            """
        ).fetchall()
        self.assertEqual(
            {row["name"] for row in tables},
            {"playgrounds", "publisher_grants"},
        )

        indexes = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name IN (
                'playgrounds_creator_id_idx',
                'publisher_grants_granted_by_idx'
            )
            """
        ).fetchall()
        self.assertEqual(
            {row["name"] for row in indexes},
            {"playgrounds_creator_id_idx", "publisher_grants_granted_by_idx"},
        )

    def test_version_four_database_is_upgraded(self):
        self.create_version_four_database()

        connection = self.open()

        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual([row["version"] for row in versions], [1, 2, 3, 4, 5])
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 5"
            ).fetchone()[0],
            1,
        )
        users = connection.execute("SELECT id FROM users ORDER BY id").fetchall()
        self.assertEqual([row["id"] for row in users], ["student-1", "teacher-1"])
        sessions = connection.execute(
            "SELECT token_hash FROM sessions"
        ).fetchall()
        self.assertEqual([row["token_hash"] for row in sessions], ["token-hash"])
        classes = connection.execute("SELECT id, name FROM classes").fetchall()
        self.assertEqual(
            [(row["id"], row["name"]) for row in classes],
            [("class-1", "Algebra")],
        )
        memberships = connection.execute(
            "SELECT student_id, class_id FROM class_memberships"
        ).fetchall()
        self.assertEqual(
            [(row["student_id"], row["class_id"]) for row in memberships],
            [("student-1", "class-1")],
        )

    def test_playground_policy_defaults_to_release_only(self):
        connection = self.open()
        self.insert_user(connection, user_id="teacher-1", username="teacher")
        connection.execute(
            """
            INSERT INTO playgrounds (id, creator_id, created_at)
            VALUES ('playground-1', 'teacher-1', '2026-09-15T00:00:00Z')
            """
        )

        policy = connection.execute(
            "SELECT policy FROM playgrounds WHERE id = 'playground-1'"
        ).fetchone()["policy"]
        self.assertEqual(policy, "release_only")

    def test_playground_policy_is_constrained(self):
        connection = self.open()
        self.insert_user(connection, user_id="teacher-1", username="teacher")

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_playground(connection, policy="everyone")

    def test_playground_requires_existing_creator(self):
        connection = self.open()

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_playground(connection, creator_id="missing")

    def test_publisher_grant_requires_existing_user_and_grantor(self):
        connection = self.open()
        self.insert_user(connection, user_id="admin-1", username="admin")

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_publisher_grant(connection, user_id="missing")

        self.insert_user(connection, user_id="user-1", username="alice")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_publisher_grant(connection, granted_by="missing")

    def test_publisher_grant_has_at_most_one_row_per_user(self):
        connection = self.open()
        self.insert_user(connection, user_id="admin-1", username="admin")
        self.insert_user(connection, user_id="user-1", username="alice")
        self.insert_publisher_grant(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_publisher_grant(connection, granted_by="admin-1")

    def test_deleting_playground_creator_is_restricted(self):
        connection = self.open()
        self.insert_user(connection, user_id="teacher-1", username="teacher")
        self.insert_playground(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id = 'teacher-1'")

    def test_deleting_publisher_grant_owner_is_restricted(self):
        connection = self.open()
        self.insert_user(connection, user_id="admin-1", username="admin")
        self.insert_user(connection, user_id="user-1", username="alice")
        self.insert_publisher_grant(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id = 'user-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id = 'admin-1'")

    def test_playground_tables_store_no_git_content(self):
        connection = self.open()
        forbidden = {
            "source",
            "blob",
            "tree",
            "commit",
            "commit_id",
            "content",
            "payload",
        }

        for table in ("playgrounds", "publisher_grants"):
            columns = connection.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
            self.assertEqual(
                {row["name"] for row in columns} & forbidden,
                set(),
                f"unexpected Git content columns in {table}",
            )

    def test_class_name_must_not_be_empty(self):
        connection = self.open()
        self.insert_user(connection, user_id="teacher-1", username="teacher")

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_class(connection, name="")

    def test_class_requires_existing_teacher(self):
        connection = self.open()

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_class(connection, teacher_id="missing")

    def test_membership_requires_existing_student_and_class(self):
        connection = self.open()
        self.insert_user(connection, user_id="teacher-1", username="teacher")
        self.insert_user(connection, user_id="student-1", username="student")
        self.insert_class(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_membership(connection, "missing", "class-1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_membership(connection, "student-1", "missing")

    def test_student_has_at_most_one_membership(self):
        connection = self.open()
        self.insert_user(connection, user_id="teacher-1", username="teacher")
        self.insert_user(connection, user_id="student-1", username="student")
        self.insert_class(connection, class_id="class-1")
        self.insert_class(connection, class_id="class-2")
        self.insert_membership(connection, "student-1", "class-1")

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_membership(connection, "student-1", "class-2")

    def test_deleting_class_owner_is_restricted(self):
        connection = self.open()
        self.insert_user(connection, user_id="teacher-1", username="teacher")
        self.insert_class(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id = 'teacher-1'")

    def test_deleting_class_with_membership_is_restricted(self):
        connection = self.open()
        self.insert_user(connection, user_id="teacher-1", username="teacher")
        self.insert_user(connection, user_id="student-1", username="student")
        self.insert_class(connection)
        self.insert_membership(connection, "student-1", "class-1")

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM classes WHERE id = 'class-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id = 'student-1'")

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

    def test_session_requires_user(self):
        connection = self.open()

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO sessions (token_hash, user_id, created_at, expires_at)
                VALUES ('token-hash', 'missing', '2026-09-15T00:00:00Z',
                        '2026-09-16T00:00:00Z')
                """
            )

    def test_session_stores_unique_token_hash(self):
        connection = self.open()
        self.insert_user(connection)
        values = (
            "token-hash",
            "user-1",
            "2026-09-15T00:00:00Z",
            "2026-09-16T00:00:00Z",
        )
        connection.execute(
            """
            INSERT INTO sessions (token_hash, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            values,
        )

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO sessions (token_hash, user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                values,
            )

        columns = connection.execute("PRAGMA table_info(sessions)").fetchall()
        self.assertNotIn("token", {row["name"] for row in columns})


if __name__ == "__main__":
    unittest.main(verbosity=2)
