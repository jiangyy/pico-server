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

    def insert_idempotency_result(
        self,
        connection,
        actor_id="user-1",
        endpoint="/playgrounds",
        request_key="key-1",
        request_sha256="a" * 64,
        status=201,
        response_json='{"id":"playground-1"}',
    ):
        connection.execute(
            """
            INSERT INTO idempotency_results (
                actor_id, endpoint, request_key, request_sha256,
                response_status, response_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, '2026-09-15T00:00:00Z')
            """,
            (
                actor_id,
                endpoint,
                request_key,
                request_sha256,
                status,
                response_json,
            ),
        )

    def insert_release(
        self,
        connection,
        release_id="release-1",
        class_id="class-1",
        playground_id="playground-1",
        playground_commit_id="commit-1",
        created_by="teacher-1",
        open_at="2026-09-20T00:00:00Z",
        due_at=None,
        requires_submission=0,
        created_at="2026-09-15T00:00:00Z",
    ):
        connection.execute(
            """
            INSERT INTO releases (
                id, class_id, playground_id, playground_commit_id,
                created_by, open_at, due_at, requires_submission, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                release_id,
                class_id,
                playground_id,
                playground_commit_id,
                created_by,
                open_at,
                due_at,
                requires_submission,
                created_at,
            ),
        )

    def insert_release_parents(self, connection):
        self.insert_user(connection, user_id="teacher-1", username="teacher")
        self.insert_class(
            connection, class_id="class-1", teacher_id="teacher-1"
        )
        self.insert_playground(
            connection, playground_id="playground-1", creator_id="teacher-1"
        )

    def insert_submission(
        self,
        connection,
        submission_id="submission-1",
        release_id="release-1",
        student_id="student-1",
        work_commit_id="work-commit-1",
        created_at="2026-09-21T00:00:00Z",
    ):
        connection.execute(
            """
            INSERT INTO submissions (
                id, release_id, student_id, work_commit_id, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                submission_id,
                release_id,
                student_id,
                work_commit_id,
                created_at,
            ),
        )

    def insert_submission_parents(self, connection):
        self.insert_release_parents(connection)
        self.insert_user(connection, user_id="student-1", username="student")
        self.insert_release(connection)

    def insert_comment(
        self,
        connection,
        comment_id="comment-1",
        submission_id="submission-1",
        author_user_id="teacher-1",
        source="teacher",
        body="Nice work",
        created_at="2026-09-22T00:00:00Z",
    ):
        connection.execute(
            """
            INSERT INTO comments (
                id, submission_id, author_user_id, source, body, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                comment_id,
                submission_id,
                author_user_id,
                source,
                body,
                created_at,
            ),
        )

    def insert_comment_parents(self, connection):
        self.insert_submission_parents(connection)
        self.insert_submission(connection)

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

    def create_version_six_database(self):
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
                (4, '2026-09-15T00:00:03Z'),
                (5, '2026-09-15T00:00:04Z'),
                (6, '2026-09-15T00:00:05Z');
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
            CREATE TABLE playgrounds (
                id TEXT PRIMARY KEY,
                creator_id TEXT NOT NULL
                    REFERENCES users(id) ON DELETE RESTRICT,
                policy TEXT NOT NULL DEFAULT 'release_only'
                    CHECK (policy IN ('release_only', 'public')),
                created_at TEXT NOT NULL,
                archived_at TEXT
            );
            CREATE TABLE publisher_grants (
                user_id TEXT PRIMARY KEY
                    REFERENCES users(id) ON DELETE RESTRICT,
                granted_by TEXT NOT NULL
                    REFERENCES users(id) ON DELETE RESTRICT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX playgrounds_creator_id_idx ON playgrounds(creator_id);
            CREATE INDEX publisher_grants_granted_by_idx
                ON publisher_grants(granted_by);
            CREATE TABLE idempotency_results (
                actor_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                endpoint TEXT NOT NULL
                    CHECK (endpoint <> '' AND length(endpoint) <= 255),
                request_key TEXT NOT NULL
                    CHECK (request_key <> '' AND length(request_key) <= 255),
                request_sha256 TEXT NOT NULL
                    CHECK (length(request_sha256) = 64),
                response_status INTEGER NOT NULL
                    CHECK (response_status BETWEEN 100 AND 599),
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (actor_id, endpoint, request_key)
            );
            INSERT INTO users (id, username, role, password_hash, created_at)
            VALUES
                ('teacher-1', 'teacher', 'teacher', 'hash-1',
                 '2026-09-15T00:00:00Z'),
                ('student-1', 'student', 'student', 'hash-2',
                 '2026-09-15T00:00:00Z'),
                ('admin-1', 'admin', 'admin', 'hash-3',
                 '2026-09-15T00:00:00Z');
            INSERT INTO sessions (token_hash, user_id, created_at, expires_at)
            VALUES ('token-hash', 'teacher-1', '2026-09-15T00:00:00Z',
                    '2026-09-16T00:00:00Z');
            INSERT INTO classes (id, name, teacher_id, created_at)
            VALUES ('class-1', 'Algebra', 'teacher-1',
                    '2026-09-15T00:00:00Z');
            INSERT INTO class_memberships (student_id, class_id, joined_at)
            VALUES ('student-1', 'class-1', '2026-09-15T00:00:00Z');
            INSERT INTO playgrounds (id, creator_id, policy, created_at)
            VALUES ('playground-1', 'teacher-1', 'release_only',
                    '2026-09-15T00:00:00Z');
            INSERT INTO publisher_grants (user_id, granted_by, created_at)
            VALUES ('teacher-1', 'admin-1', '2026-09-15T00:00:00Z');
            INSERT INTO idempotency_results (
                actor_id, endpoint, request_key, request_sha256,
                response_status, response_json, created_at
            ) VALUES (
                'teacher-1', '/playgrounds', 'key-1',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                201, '{"id":"playground-1"}', '2026-09-15T00:00:00Z'
            );
            """
        )
        connection.close()

    def create_version_seven_database(self):
        self.create_version_six_database()
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE releases (
                id TEXT PRIMARY KEY NOT NULL,
                class_id TEXT NOT NULL
                    REFERENCES classes(id) ON DELETE RESTRICT,
                playground_id TEXT NOT NULL
                    REFERENCES playgrounds(id) ON DELETE RESTRICT,
                playground_commit_id TEXT NOT NULL
                    CHECK (playground_commit_id <> ''),
                created_by TEXT NOT NULL
                    REFERENCES users(id) ON DELETE RESTRICT,
                open_at TEXT NOT NULL,
                due_at TEXT,
                requires_submission INTEGER NOT NULL
                    CHECK (requires_submission IN (0, 1)),
                created_at TEXT NOT NULL
            );
            CREATE INDEX releases_class_id_idx ON releases(class_id);
            CREATE INDEX releases_playground_commit_id_idx
                ON releases(playground_id, playground_commit_id);
            INSERT INTO schema_migrations (version, applied_at)
            VALUES (7, '2026-09-15T00:00:06Z');
            INSERT INTO releases (
                id, class_id, playground_id, playground_commit_id,
                created_by, open_at, due_at, requires_submission, created_at
            ) VALUES (
                'release-1', 'class-1', 'playground-1', 'commit-1',
                'teacher-1', '2026-09-20T00:00:00Z', NULL, 1,
                '2026-09-15T00:00:00Z'
            );
            """
        )
        connection.close()

    def create_version_eight_database(self):
        self.create_version_seven_database()
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE submissions (
                id TEXT PRIMARY KEY NOT NULL,
                release_id TEXT NOT NULL
                    REFERENCES releases(id) ON DELETE RESTRICT,
                student_id TEXT NOT NULL
                    REFERENCES users(id) ON DELETE RESTRICT,
                work_commit_id TEXT NOT NULL
                    CHECK (work_commit_id <> ''),
                created_at TEXT NOT NULL
            );
            CREATE INDEX submissions_release_id_idx ON submissions(release_id);
            CREATE INDEX submissions_student_id_idx ON submissions(student_id);
            INSERT INTO schema_migrations (version, applied_at)
            VALUES (8, '2026-09-15T00:00:07Z');
            INSERT INTO submissions (
                id, release_id, student_id, work_commit_id, created_at
            ) VALUES
                ('submission-1', 'release-1', 'student-1', 'work-commit-1',
                 '2026-09-21T00:00:00Z'),
                ('submission-2', 'release-1', 'student-1', 'work-commit-2',
                 '2026-09-21T00:00:01Z');
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
        self.assertEqual(
            [row["version"] for row in versions],
            list(range(1, SCHEMA_VERSION + 1)),
        )
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
        self.assertEqual(
            [row["version"] for row in versions],
            list(range(1, SCHEMA_VERSION + 1)),
        )
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

    def test_version_five_database_is_upgraded(self):
        connection = self.open()
        self.insert_user(connection)
        connection.execute("DROP TABLE comments")
        connection.execute("DROP TABLE submissions")
        connection.execute("DROP TABLE releases")
        connection.execute("DROP TABLE idempotency_results")
        connection.execute("DELETE FROM schema_migrations WHERE version = 9")
        connection.execute("DELETE FROM schema_migrations WHERE version = 8")
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")
        connection.execute("DELETE FROM schema_migrations WHERE version = 6")
        connection.commit()

        upgraded = self.open()

        versions = upgraded.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual(
            [row["version"] for row in versions],
            list(range(1, SCHEMA_VERSION + 1)),
        )
        users = upgraded.execute("SELECT id FROM users").fetchall()
        self.assertEqual([row["id"] for row in users], ["user-1"])
        tables = upgraded.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name IN (
                'idempotency_results', 'releases', 'submissions', 'comments'
            )
            ORDER BY name
            """
        ).fetchall()
        self.assertEqual(
            [row["name"] for row in tables],
            ["comments", "idempotency_results", "releases", "submissions"],
        )

    def test_idempotency_schema_is_created(self):
        connection = self.open()

        columns = connection.execute(
            "PRAGMA table_info(idempotency_results)"
        ).fetchall()
        self.assertEqual(
            {row["name"] for row in columns},
            {
                "actor_id",
                "endpoint",
                "request_key",
                "request_sha256",
                "response_status",
                "response_json",
                "created_at",
            },
        )
        primary_key = [row["name"] for row in columns if row["pk"] > 0]
        self.assertEqual(primary_key, ["actor_id", "endpoint", "request_key"])

    def test_idempotency_scope_must_be_unique(self):
        connection = self.open()
        self.insert_user(connection)
        self.insert_idempotency_result(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_idempotency_result(connection, status=202)

    def test_idempotency_requires_existing_actor(self):
        connection = self.open()

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_idempotency_result(connection, actor_id="missing")

    def test_idempotency_rejects_empty_or_oversized_scope(self):
        connection = self.open()
        self.insert_user(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_idempotency_result(connection, endpoint="")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_idempotency_result(connection, request_key="")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_idempotency_result(connection, endpoint="e" * 256)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_idempotency_result(connection, request_key="k" * 256)

    def test_idempotency_rejects_invalid_status_and_digest(self):
        connection = self.open()
        self.insert_user(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_idempotency_result(connection, status=99)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_idempotency_result(connection, status=600)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_idempotency_result(connection, request_sha256="short")

    def test_idempotency_stores_no_secret_columns(self):
        connection = self.open()
        forbidden = {
            "token",
            "token_hash",
            "password",
            "password_hash",
            "source",
            "body",
            "request_body",
            "bundle",
        }

        columns = connection.execute(
            "PRAGMA table_info(idempotency_results)"
        ).fetchall()
        self.assertEqual(
            {row["name"] for row in columns} & forbidden,
            set(),
            "unexpected secret columns in idempotency_results",
        )

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

    def test_version_six_database_is_upgraded(self):
        self.create_version_six_database()

        connection = self.open()

        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual(
            [row["version"] for row in versions],
            list(range(1, SCHEMA_VERSION + 1)),
        )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 7"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0], 0
        )
        users = connection.execute("SELECT id FROM users ORDER BY id").fetchall()
        self.assertEqual(
            [row["id"] for row in users], ["admin-1", "student-1", "teacher-1"]
        )
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
        playgrounds = connection.execute(
            "SELECT id, creator_id, policy FROM playgrounds"
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["creator_id"], row["policy"]) for row in playgrounds],
            [("playground-1", "teacher-1", "release_only")],
        )
        grants = connection.execute(
            "SELECT user_id, granted_by FROM publisher_grants"
        ).fetchall()
        self.assertEqual(
            [(row["user_id"], row["granted_by"]) for row in grants],
            [("teacher-1", "admin-1")],
        )
        idempotency = connection.execute(
            """
            SELECT actor_id, endpoint, request_key, response_status
            FROM idempotency_results
            """
        ).fetchall()
        self.assertEqual(
            [
                (
                    row["actor_id"],
                    row["endpoint"],
                    row["request_key"],
                    row["response_status"],
                )
                for row in idempotency
            ],
            [("teacher-1", "/playgrounds", "key-1", 201)],
        )

    def test_failed_release_migration_rolls_back(self):
        self.create_version_six_database()
        blocker = sqlite3.connect(self.database_path)
        blocker.execute("CREATE TABLE releases_class_id_idx (marker INTEGER)")
        blocker.commit()
        blocker.close()

        with self.assertRaises(sqlite3.OperationalError):
            open_database(self.database_path)

        connection = sqlite3.connect(self.database_path)
        self.addCleanup(connection.close)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertNotIn("releases", tables)
        self.assertIn("releases_class_id_idx", tables)
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        self.assertEqual(versions, [1, 2, 3, 4, 5, 6])

    def test_release_schema_is_created(self):
        connection = self.open()

        columns = connection.execute("PRAGMA table_info(releases)").fetchall()
        self.assertEqual(
            {row["name"] for row in columns},
            {
                "id",
                "class_id",
                "playground_id",
                "playground_commit_id",
                "created_by",
                "open_at",
                "due_at",
                "requires_submission",
                "created_at",
            },
        )
        primary_key = [row["name"] for row in columns if row["pk"] > 0]
        self.assertEqual(primary_key, ["id"])

    def test_release_required_columns_are_not_null(self):
        connection = self.open()
        required = {
            "id",
            "class_id",
            "playground_id",
            "playground_commit_id",
            "created_by",
            "open_at",
            "requires_submission",
            "created_at",
        }
        columns = connection.execute("PRAGMA table_info(releases)").fetchall()
        not_null = {row["name"] for row in columns if row["notnull"] == 1}
        self.assertEqual(not_null, required)
        optional = {row["name"] for row in columns if row["notnull"] == 0}
        self.assertEqual(optional, {"due_at"})

        self.insert_release_parents(connection)
        values = {
            "release_id": "release-1",
            "class_id": "class-1",
            "playground_id": "playground-1",
            "playground_commit_id": "commit-1",
            "created_by": "teacher-1",
            "open_at": "2026-09-20T00:00:00Z",
            "requires_submission": 0,
            "created_at": "2026-09-15T00:00:00Z",
        }
        for column, key in (
            ("id", "release_id"),
            ("class_id", "class_id"),
            ("playground_id", "playground_id"),
            ("playground_commit_id", "playground_commit_id"),
            ("created_by", "created_by"),
            ("open_at", "open_at"),
            ("requires_submission", "requires_submission"),
            ("created_at", "created_at"),
        ):
            with self.subTest(column=column):
                missing = dict(values)
                missing[key] = None
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_release(connection, **missing)

    def test_release_boolean_is_strictly_zero_or_one(self):
        connection = self.open()
        self.insert_release_parents(connection)

        self.insert_release(
            connection, release_id="release-0", requires_submission=0
        )
        self.insert_release(
            connection, release_id="release-1", requires_submission=1
        )
        stored = connection.execute(
            "SELECT id, requires_submission FROM releases ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["requires_submission"]) for row in stored],
            [("release-0", 0), ("release-1", 1)],
        )

        for invalid in (2, -1, 0.5, "yes", "true"):
            with self.subTest(value=invalid):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_release(
                        connection,
                        release_id="release-invalid",
                        requires_submission=invalid,
                    )

    def test_release_due_at_is_nullable(self):
        connection = self.open()
        self.insert_release_parents(connection)

        self.insert_release(connection, release_id="release-open", due_at=None)
        self.insert_release(
            connection,
            release_id="release-due",
            due_at="2026-10-01T00:00:00Z",
        )

        rows = connection.execute(
            "SELECT id, due_at FROM releases ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["due_at"]) for row in rows],
            [
                ("release-due", "2026-10-01T00:00:00Z"),
                ("release-open", None),
            ],
        )

    def test_release_index_shape(self):
        connection = self.open()

        indexes = {
            row["name"]: row
            for row in connection.execute("PRAGMA index_list(releases)").fetchall()
        }
        self.assertIn("releases_class_id_idx", indexes)
        self.assertIn("releases_playground_commit_id_idx", indexes)
        self.assertEqual(indexes["releases_class_id_idx"]["unique"], 0)
        self.assertEqual(
            indexes["releases_playground_commit_id_idx"]["unique"], 0
        )

        class_columns = [
            row["name"]
            for row in connection.execute(
                "PRAGMA index_info(releases_class_id_idx)"
            ).fetchall()
        ]
        self.assertEqual(class_columns, ["class_id"])
        commit_columns = [
            row["name"]
            for row in connection.execute(
                "PRAGMA index_info(releases_playground_commit_id_idx)"
            ).fetchall()
        ]
        self.assertEqual(
            commit_columns, ["playground_id", "playground_commit_id"]
        )

    def test_release_foreign_key_shape(self):
        connection = self.open()

        foreign_keys = {
            (row["from"], row["table"], row["to"], row["on_delete"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(releases)"
            ).fetchall()
        }
        self.assertEqual(
            foreign_keys,
            {
                ("class_id", "classes", "id", "RESTRICT"),
                ("playground_id", "playgrounds", "id", "RESTRICT"),
                ("created_by", "users", "id", "RESTRICT"),
            },
        )

    def test_release_requires_existing_class(self):
        connection = self.open()
        self.insert_user(connection, user_id="teacher-1", username="teacher")
        self.insert_playground(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_release(connection, class_id="missing")

    def test_release_requires_existing_playground(self):
        connection = self.open()
        self.insert_user(connection, user_id="teacher-1", username="teacher")
        self.insert_class(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_release(connection, playground_id="missing")

    def test_release_requires_existing_creator(self):
        connection = self.open()
        self.insert_user(connection, user_id="teacher-1", username="teacher")
        self.insert_class(connection)
        self.insert_playground(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_release(connection, created_by="missing")

    def test_deleting_release_references_is_restricted(self):
        connection = self.open()
        self.insert_release_parents(connection)
        self.insert_user(connection, user_id="author-1", username="author")
        self.insert_release(connection, created_by="author-1")

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM classes WHERE id = 'class-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM playgrounds WHERE id = 'playground-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id = 'author-1'")

    def test_release_commit_id_is_opaque_and_not_a_foreign_key(self):
        connection = self.open()
        self.insert_release_parents(connection)

        self.insert_release(
            connection, release_id="release-1", playground_commit_id="0" * 40
        )
        self.insert_release(
            connection, release_id="release-2", playground_commit_id="deadbeef"
        )

        foreign_columns = {
            row["from"]
            for row in connection.execute(
                "PRAGMA foreign_key_list(releases)"
            ).fetchall()
        }
        self.assertNotIn("playground_commit_id", foreign_columns)
        commits = connection.execute(
            "SELECT DISTINCT playground_commit_id FROM releases "
            "ORDER BY playground_commit_id"
        ).fetchall()
        self.assertEqual(
            [row["playground_commit_id"] for row in commits],
            ["0" * 40, "deadbeef"],
        )

    def test_release_rejects_empty_commit_id(self):
        connection = self.open()
        self.insert_release_parents(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_release(connection, playground_commit_id="")

    def test_multiple_releases_may_share_exact_playground_commit(self):
        connection = self.open()
        self.insert_release_parents(connection)

        self.insert_release(
            connection,
            release_id="release-1",
            playground_commit_id="commit-shared",
        )
        self.insert_release(
            connection,
            release_id="release-2",
            playground_commit_id="commit-shared",
        )

        rows = connection.execute(
            """
            SELECT id, playground_id, playground_commit_id
            FROM releases ORDER BY id
            """
        ).fetchall()
        self.assertEqual(
            [
                (row["id"], row["playground_id"], row["playground_commit_id"])
                for row in rows
            ],
            [
                ("release-1", "playground-1", "commit-shared"),
                ("release-2", "playground-1", "commit-shared"),
            ],
        )

    def test_release_stores_no_source_manifest_or_snapshot_columns(self):
        connection = self.open()
        forbidden = {
            "branch",
            "branch_head",
            "head",
            "head_commit",
            "head_commit_id",
            "source",
            "source_code",
            "source_commit",
            "manifest",
            "manifest_id",
            "manifest_json",
            "manifest_sha256",
            "snapshot",
            "membership_snapshot",
            "members_snapshot",
            "content",
            "blob",
            "tree",
            "payload",
            "bundle",
        }

        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(releases)").fetchall()
        }
        self.assertEqual(
            columns & forbidden,
            set(),
            "releases must not duplicate branch, source, manifest, "
            "or membership snapshot data",
        )

    def test_submission_schema_is_created(self):
        connection = self.open()

        columns = connection.execute("PRAGMA table_info(submissions)").fetchall()
        self.assertEqual(
            {row["name"] for row in columns},
            {
                "id",
                "release_id",
                "student_id",
                "work_commit_id",
                "created_at",
            },
        )
        primary_key = [row["name"] for row in columns if row["pk"] > 0]
        self.assertEqual(primary_key, ["id"])

    def test_submission_required_columns_are_not_null(self):
        connection = self.open()
        required = {
            "id",
            "release_id",
            "student_id",
            "work_commit_id",
            "created_at",
        }
        columns = connection.execute("PRAGMA table_info(submissions)").fetchall()
        not_null = {row["name"] for row in columns if row["notnull"] == 1}
        self.assertEqual(not_null, required)
        self.assertEqual(
            {row["name"] for row in columns if row["notnull"] == 0}, set()
        )

        self.insert_submission_parents(connection)
        values = {
            "submission_id": "submission-1",
            "release_id": "release-1",
            "student_id": "student-1",
            "work_commit_id": "work-commit-1",
            "created_at": "2026-09-21T00:00:00Z",
        }
        for column, key in (
            ("id", "submission_id"),
            ("release_id", "release_id"),
            ("student_id", "student_id"),
            ("work_commit_id", "work_commit_id"),
            ("created_at", "created_at"),
        ):
            with self.subTest(column=column):
                missing = dict(values)
                missing[key] = None
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_submission(connection, **missing)

    def test_submission_index_shape(self):
        connection = self.open()

        indexes = {
            row["name"]: row
            for row in connection.execute("PRAGMA index_list(submissions)").fetchall()
        }
        self.assertIn("submissions_release_id_idx", indexes)
        self.assertIn("submissions_student_id_idx", indexes)
        self.assertEqual(indexes["submissions_release_id_idx"]["unique"], 0)
        self.assertEqual(indexes["submissions_student_id_idx"]["unique"], 0)

        release_columns = [
            row["name"]
            for row in connection.execute(
                "PRAGMA index_info(submissions_release_id_idx)"
            ).fetchall()
        ]
        self.assertEqual(release_columns, ["release_id"])
        student_columns = [
            row["name"]
            for row in connection.execute(
                "PRAGMA index_info(submissions_student_id_idx)"
            ).fetchall()
        ]
        self.assertEqual(student_columns, ["student_id"])

    def test_submission_foreign_key_shape(self):
        connection = self.open()

        foreign_keys = {
            (row["from"], row["table"], row["to"], row["on_delete"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(submissions)"
            ).fetchall()
        }
        self.assertEqual(
            foreign_keys,
            {
                ("release_id", "releases", "id", "RESTRICT"),
                ("student_id", "users", "id", "RESTRICT"),
            },
        )

    def test_submission_requires_existing_release_and_student(self):
        connection = self.open()
        self.insert_submission_parents(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_submission(connection, release_id="missing")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_submission(connection, student_id="missing")

    def test_deleting_submission_references_is_restricted(self):
        connection = self.open()
        self.insert_submission_parents(connection)
        self.insert_submission(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM releases WHERE id = 'release-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id = 'student-1'")

    def test_repeated_submissions_are_allowed(self):
        connection = self.open()
        self.insert_submission_parents(connection)

        self.insert_submission(
            connection,
            submission_id="submission-1",
            work_commit_id="work-commit-1",
        )
        self.insert_submission(
            connection,
            submission_id="submission-2",
            work_commit_id="work-commit-1",
        )
        self.insert_submission(
            connection,
            submission_id="submission-3",
            work_commit_id="work-commit-2",
        )

        rows = connection.execute(
            """
            SELECT id, release_id, student_id, work_commit_id
            FROM submissions ORDER BY id
            """
        ).fetchall()
        self.assertEqual(
            [
                (
                    row["id"],
                    row["release_id"],
                    row["student_id"],
                    row["work_commit_id"],
                )
                for row in rows
            ],
            [
                ("submission-1", "release-1", "student-1", "work-commit-1"),
                ("submission-2", "release-1", "student-1", "work-commit-1"),
                ("submission-3", "release-1", "student-1", "work-commit-2"),
            ],
        )

    def test_submission_work_commit_id_is_opaque_and_not_a_foreign_key(self):
        connection = self.open()
        self.insert_submission_parents(connection)

        self.insert_submission(
            connection,
            submission_id="submission-1",
            work_commit_id="0" * 40,
        )
        self.insert_submission(
            connection,
            submission_id="submission-2",
            work_commit_id="deadbeef",
        )

        foreign_columns = {
            row["from"]
            for row in connection.execute(
                "PRAGMA foreign_key_list(submissions)"
            ).fetchall()
        }
        self.assertNotIn("work_commit_id", foreign_columns)
        commits = connection.execute(
            "SELECT DISTINCT work_commit_id FROM submissions "
            "ORDER BY work_commit_id"
        ).fetchall()
        self.assertEqual(
            [row["work_commit_id"] for row in commits],
            ["0" * 40, "deadbeef"],
        )

    def test_submission_rejects_empty_work_commit_id(self):
        connection = self.open()
        self.insert_submission_parents(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_submission(connection, work_commit_id="")

    def test_submission_stores_no_source_tree_or_status_columns(self):
        connection = self.open()
        forbidden = {
            "source",
            "source_code",
            "tree",
            "blob",
            "content",
            "payload",
            "bundle",
            "status",
            "state",
            "grading_status",
            "submission_status",
        }

        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(submissions)").fetchall()
        }
        self.assertEqual(
            columns & forbidden,
            set(),
            "submissions must not store source, tree, or mutable status data",
        )

    def test_version_seven_database_is_upgraded(self):
        self.create_version_seven_database()

        connection = self.open()

        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual(
            [row["version"] for row in versions],
            list(range(1, SCHEMA_VERSION + 1)),
        )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 8"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM submissions").fetchone()[0],
            0,
        )
        users = connection.execute(
            "SELECT id, username, role FROM users ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [
                (row["id"], row["username"], row["role"])
                for row in users
            ],
            [
                ("admin-1", "admin", "admin"),
                ("student-1", "student", "student"),
                ("teacher-1", "teacher", "teacher"),
            ],
        )
        sessions = connection.execute(
            """
            SELECT token_hash, user_id, created_at, expires_at, revoked_at
            FROM sessions
            """
        ).fetchall()
        self.assertEqual(
            [
                (
                    row["token_hash"],
                    row["user_id"],
                    row["created_at"],
                    row["expires_at"],
                    row["revoked_at"],
                )
                for row in sessions
            ],
            [
                (
                    "token-hash",
                    "teacher-1",
                    "2026-09-15T00:00:00Z",
                    "2026-09-16T00:00:00Z",
                    None,
                )
            ],
        )
        classes = connection.execute(
            "SELECT id, name, teacher_id, created_at, archived_at FROM classes"
        ).fetchall()
        self.assertEqual(
            [
                (
                    row["id"],
                    row["name"],
                    row["teacher_id"],
                    row["created_at"],
                    row["archived_at"],
                )
                for row in classes
            ],
            [
                (
                    "class-1",
                    "Algebra",
                    "teacher-1",
                    "2026-09-15T00:00:00Z",
                    None,
                )
            ],
        )
        memberships = connection.execute(
            "SELECT student_id, class_id, joined_at FROM class_memberships"
        ).fetchall()
        self.assertEqual(
            [
                (row["student_id"], row["class_id"], row["joined_at"])
                for row in memberships
            ],
            [("student-1", "class-1", "2026-09-15T00:00:00Z")],
        )
        playgrounds = connection.execute(
            """
            SELECT id, creator_id, policy, created_at, archived_at
            FROM playgrounds
            """
        ).fetchall()
        self.assertEqual(
            [
                (
                    row["id"],
                    row["creator_id"],
                    row["policy"],
                    row["created_at"],
                    row["archived_at"],
                )
                for row in playgrounds
            ],
            [
                (
                    "playground-1",
                    "teacher-1",
                    "release_only",
                    "2026-09-15T00:00:00Z",
                    None,
                )
            ],
        )
        grants = connection.execute(
            "SELECT user_id, granted_by, created_at FROM publisher_grants"
        ).fetchall()
        self.assertEqual(
            [
                (row["user_id"], row["granted_by"], row["created_at"])
                for row in grants
            ],
            [("teacher-1", "admin-1", "2026-09-15T00:00:00Z")],
        )
        idempotency = connection.execute(
            """
            SELECT actor_id, endpoint, request_key, request_sha256,
                   response_status, response_json, created_at
            FROM idempotency_results
            """
        ).fetchall()
        self.assertEqual(
            [
                (
                    row["actor_id"],
                    row["endpoint"],
                    row["request_key"],
                    row["request_sha256"],
                    row["response_status"],
                    row["response_json"],
                    row["created_at"],
                )
                for row in idempotency
            ],
            [
                (
                    "teacher-1",
                    "/playgrounds",
                    "key-1",
                    "a" * 64,
                    201,
                    '{"id":"playground-1"}',
                    "2026-09-15T00:00:00Z",
                )
            ],
        )
        releases = connection.execute(
            """
            SELECT id, class_id, playground_id, playground_commit_id,
                   created_by, open_at, due_at, requires_submission, created_at
            FROM releases
            """
        ).fetchall()
        self.assertEqual(
            [
                (
                    row["id"],
                    row["class_id"],
                    row["playground_id"],
                    row["playground_commit_id"],
                    row["created_by"],
                    row["open_at"],
                    row["due_at"],
                    row["requires_submission"],
                    row["created_at"],
                )
                for row in releases
            ],
            [
                (
                    "release-1",
                    "class-1",
                    "playground-1",
                    "commit-1",
                    "teacher-1",
                    "2026-09-20T00:00:00Z",
                    None,
                    1,
                    "2026-09-15T00:00:00Z",
                )
            ],
        )

    def test_submission_survives_account_and_class_administration(self):
        connection = self.open()
        self.insert_submission_parents(connection)
        self.insert_membership(connection, "student-1", "class-1")
        self.insert_submission(connection)

        before = dict(
            connection.execute(
                """
                SELECT id, release_id, student_id, work_commit_id, created_at
                FROM submissions WHERE id = 'submission-1'
                """
            ).fetchone()
        )

        connection.execute(
            "UPDATE users SET username = 'student-renamed' WHERE id = 'student-1'"
        )
        self.insert_user(connection, user_id="teacher-2", username="teacher-two")
        connection.execute(
            "UPDATE classes SET teacher_id = 'teacher-2' WHERE id = 'class-1'"
        )
        self.insert_class(
            connection, class_id="class-2", teacher_id="teacher-2", name="Geometry"
        )
        connection.execute(
            "UPDATE class_memberships SET class_id = 'class-2' "
            "WHERE student_id = 'student-1'"
        )
        connection.execute(
            "DELETE FROM class_memberships WHERE student_id = 'student-1'"
        )

        self.assertEqual(
            connection.execute(
                "SELECT username FROM users WHERE id = 'student-1'"
            ).fetchone()["username"],
            "student-renamed",
        )
        self.assertEqual(
            connection.execute(
                "SELECT teacher_id FROM classes WHERE id = 'class-1'"
            ).fetchone()["teacher_id"],
            "teacher-2",
        )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM class_memberships"
            ).fetchone()[0],
            0,
        )

        after = dict(
            connection.execute(
                """
                SELECT id, release_id, student_id, work_commit_id, created_at
                FROM submissions WHERE id = 'submission-1'
                """
            ).fetchone()
        )
        self.assertEqual(
            after,
            {
                "id": "submission-1",
                "release_id": "release-1",
                "student_id": "student-1",
                "work_commit_id": "work-commit-1",
                "created_at": "2026-09-21T00:00:00Z",
            },
        )
        self.assertEqual(after, before)

    def test_failed_submission_migration_rolls_back(self):
        self.create_version_seven_database()
        blocker = sqlite3.connect(self.database_path)
        blocker.execute("CREATE TABLE submissions_release_id_idx (marker INTEGER)")
        blocker.commit()
        blocker.close()

        with self.assertRaises(sqlite3.OperationalError):
            open_database(self.database_path)

        connection = sqlite3.connect(self.database_path)
        self.addCleanup(connection.close)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertNotIn("submissions", tables)
        self.assertIn("submissions_release_id_idx", tables)
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7])

    def test_comment_schema_is_created(self):
        connection = self.open()

        columns = connection.execute("PRAGMA table_info(comments)").fetchall()
        self.assertEqual(
            {row["name"] for row in columns},
            {
                "id",
                "submission_id",
                "author_user_id",
                "source",
                "body",
                "created_at",
            },
        )
        primary_key = [row["name"] for row in columns if row["pk"] > 0]
        self.assertEqual(primary_key, ["id"])

    def test_comment_required_columns_are_not_null(self):
        connection = self.open()
        required = {"id", "submission_id", "source", "body", "created_at"}
        columns = connection.execute("PRAGMA table_info(comments)").fetchall()
        not_null = {row["name"] for row in columns if row["notnull"] == 1}
        self.assertEqual(not_null, required)
        self.assertEqual(
            {row["name"] for row in columns if row["notnull"] == 0},
            {"author_user_id"},
        )

        self.insert_comment_parents(connection)
        values = {
            "comment_id": "comment-1",
            "submission_id": "submission-1",
            "author_user_id": "teacher-1",
            "source": "teacher",
            "body": "Nice work",
            "created_at": "2026-09-22T00:00:00Z",
        }
        for column, key in (
            ("id", "comment_id"),
            ("submission_id", "submission_id"),
            ("source", "source"),
            ("body", "body"),
            ("created_at", "created_at"),
        ):
            with self.subTest(column=column):
                missing = dict(values)
                missing[key] = None
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_comment(connection, **missing)

    def test_comment_foreign_key_shape(self):
        connection = self.open()

        foreign_keys = {
            (row["from"], row["table"], row["to"], row["on_delete"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(comments)"
            ).fetchall()
        }
        self.assertEqual(
            foreign_keys,
            {
                ("submission_id", "submissions", "id", "RESTRICT"),
                ("author_user_id", "users", "id", "RESTRICT"),
            },
        )

    def test_comment_index_shape(self):
        connection = self.open()

        indexes = {
            row["name"]: row
            for row in connection.execute("PRAGMA index_list(comments)").fetchall()
        }
        self.assertIn("comments_submission_id_created_at_idx", indexes)
        self.assertEqual(
            indexes["comments_submission_id_created_at_idx"]["unique"], 0
        )
        self.assertEqual(
            indexes["comments_submission_id_created_at_idx"]["partial"], 0
        )

        columns = [
            row["name"]
            for row in connection.execute(
                "PRAGMA index_info(comments_submission_id_created_at_idx)"
            ).fetchall()
        ]
        self.assertEqual(columns, ["submission_id", "created_at", "id"])

    def test_comment_source_is_constrained(self):
        connection = self.open()
        self.insert_comment_parents(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_comment(connection, source="admin")

        for source, author in (
            ("teacher", "teacher-1"),
            ("ai", None),
            ("system", None),
        ):
            with self.subTest(source=source):
                self.insert_comment(
                    connection,
                    comment_id=f"comment-{source}",
                    source=source,
                    author_user_id=author,
                )

        stored = connection.execute(
            "SELECT id, source FROM comments ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["source"]) for row in stored],
            [
                ("comment-ai", "ai"),
                ("comment-system", "system"),
                ("comment-teacher", "teacher"),
            ],
        )

    def test_teacher_comment_requires_author_user(self):
        connection = self.open()
        self.insert_comment_parents(connection)

        self.insert_comment(
            connection, comment_id="comment-authored", source="teacher"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_comment(
                connection,
                comment_id="comment-unauthored",
                source="teacher",
                author_user_id=None,
            )

        authors = connection.execute(
            "SELECT id, author_user_id FROM comments"
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["author_user_id"]) for row in authors],
            [("comment-authored", "teacher-1")],
        )

    def test_ai_and_system_comments_allow_optional_author(self):
        connection = self.open()
        self.insert_comment_parents(connection)

        for source in ("ai", "system"):
            with self.subTest(source=source, author=None):
                self.insert_comment(
                    connection,
                    comment_id=f"{source}-anonymous",
                    source=source,
                    author_user_id=None,
                )
            with self.subTest(source=source, author="teacher-1"):
                self.insert_comment(
                    connection,
                    comment_id=f"{source}-attributed",
                    source=source,
                    author_user_id="teacher-1",
                )

        stored = connection.execute(
            "SELECT id, source, author_user_id FROM comments ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [
                (row["id"], row["source"], row["author_user_id"])
                for row in stored
            ],
            [
                ("ai-anonymous", "ai", None),
                ("ai-attributed", "ai", "teacher-1"),
                ("system-anonymous", "system", None),
                ("system-attributed", "system", "teacher-1"),
            ],
        )

    def test_comment_body_must_not_be_empty(self):
        connection = self.open()
        self.insert_comment_parents(connection)

        self.insert_comment(connection, comment_id="comment-ok", body="Feedback")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_comment(connection, comment_id="comment-empty", body="")

        bodies = connection.execute(
            "SELECT id, body FROM comments ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["body"]) for row in bodies],
            [("comment-ok", "Feedback")],
        )

    def test_comment_requires_existing_submission_and_author(self):
        connection = self.open()
        self.insert_comment_parents(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_comment(connection, submission_id="missing")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_comment(connection, author_user_id="missing")

        self.insert_comment(
            connection,
            comment_id="comment-anonymous",
            source="system",
            author_user_id=None,
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 1
        )

    def test_deleting_comment_references_is_restricted(self):
        connection = self.open()
        self.insert_comment_parents(connection)
        self.insert_comment(connection)

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM submissions WHERE id = 'submission-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id = 'teacher-1'")
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 1
        )

    def test_multiple_comments_are_stable_with_equal_timestamps(self):
        connection = self.open()
        self.insert_comment_parents(connection)
        self.insert_user(connection, user_id="teacher-2", username="teacher-two")

        for comment_id, author, source in (
            ("comment-b", "teacher-1", "teacher"),
            ("comment-a", "teacher-2", "teacher"),
            ("comment-c", None, "system"),
        ):
            self.insert_comment(
                connection,
                comment_id=comment_id,
                author_user_id=author,
                source=source,
                created_at="2026-09-22T00:00:00Z",
            )
        self.insert_comment(
            connection,
            comment_id="comment-d",
            source="system",
            author_user_id=None,
            created_at="2026-09-23T00:00:00Z",
        )

        rows = connection.execute(
            """
            SELECT id, submission_id, author_user_id, source, body, created_at
            FROM comments ORDER BY submission_id, created_at, id
            """
        ).fetchall()
        self.assertEqual(
            [
                (
                    row["id"],
                    row["submission_id"],
                    row["author_user_id"],
                    row["source"],
                    row["body"],
                    row["created_at"],
                )
                for row in rows
            ],
            [
                (
                    "comment-a",
                    "submission-1",
                    "teacher-2",
                    "teacher",
                    "Nice work",
                    "2026-09-22T00:00:00Z",
                ),
                (
                    "comment-b",
                    "submission-1",
                    "teacher-1",
                    "teacher",
                    "Nice work",
                    "2026-09-22T00:00:00Z",
                ),
                (
                    "comment-c",
                    "submission-1",
                    None,
                    "system",
                    "Nice work",
                    "2026-09-22T00:00:00Z",
                ),
                (
                    "comment-d",
                    "submission-1",
                    None,
                    "system",
                    "Nice work",
                    "2026-09-23T00:00:00Z",
                ),
            ],
        )

    def test_comment_stores_no_mutable_or_replacement_columns(self):
        connection = self.open()
        forbidden = {
            "updated_at",
            "edited_at",
            "edit",
            "edited",
            "edits",
            "revision",
            "revision_id",
            "replacement",
            "replacement_id",
            "replaced",
            "replaced_by",
            "deleted_at",
            "deleted",
            "status",
            "state",
            "version",
            "supersedes",
            "superseded_by",
        }

        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(comments)").fetchall()
        }
        self.assertEqual(
            columns & forbidden,
            set(),
            "comments must stay immutable with no edit or replacement columns",
        )

    def test_comment_initialization_is_repeatable(self):
        self.open().close()
        connection = self.open()

        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual(
            [row["version"] for row in versions],
            list(range(1, SCHEMA_VERSION + 1)),
        )
        columns = connection.execute("PRAGMA table_info(comments)").fetchall()
        self.assertEqual(
            {row["name"] for row in columns},
            {
                "id",
                "submission_id",
                "author_user_id",
                "source",
                "body",
                "created_at",
            },
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 0
        )

    def test_version_eight_database_is_upgraded(self):
        self.create_version_eight_database()

        connection = self.open()

        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual(
            [row["version"] for row in versions],
            list(range(1, SCHEMA_VERSION + 1)),
        )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 9"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 0
        )

        users = connection.execute(
            "SELECT id, username, role FROM users ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["username"], row["role"]) for row in users],
            [
                ("admin-1", "admin", "admin"),
                ("student-1", "student", "student"),
                ("teacher-1", "teacher", "teacher"),
            ],
        )
        sessions = connection.execute(
            "SELECT token_hash, user_id, expires_at FROM sessions"
        ).fetchall()
        self.assertEqual(
            [
                (row["token_hash"], row["user_id"], row["expires_at"])
                for row in sessions
            ],
            [("token-hash", "teacher-1", "2026-09-16T00:00:00Z")],
        )
        classes = connection.execute(
            "SELECT id, name, teacher_id, archived_at FROM classes"
        ).fetchall()
        self.assertEqual(
            [
                (row["id"], row["name"], row["teacher_id"], row["archived_at"])
                for row in classes
            ],
            [("class-1", "Algebra", "teacher-1", None)],
        )
        memberships = connection.execute(
            "SELECT student_id, class_id, joined_at FROM class_memberships"
        ).fetchall()
        self.assertEqual(
            [
                (row["student_id"], row["class_id"], row["joined_at"])
                for row in memberships
            ],
            [("student-1", "class-1", "2026-09-15T00:00:00Z")],
        )
        playgrounds = connection.execute(
            "SELECT id, creator_id, policy FROM playgrounds"
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["creator_id"], row["policy"]) for row in playgrounds],
            [("playground-1", "teacher-1", "release_only")],
        )
        grants = connection.execute(
            "SELECT user_id, granted_by FROM publisher_grants"
        ).fetchall()
        self.assertEqual(
            [(row["user_id"], row["granted_by"]) for row in grants],
            [("teacher-1", "admin-1")],
        )
        idempotency = connection.execute(
            "SELECT actor_id, endpoint, request_key FROM idempotency_results"
        ).fetchall()
        self.assertEqual(
            [
                (row["actor_id"], row["endpoint"], row["request_key"])
                for row in idempotency
            ],
            [("teacher-1", "/playgrounds", "key-1")],
        )
        releases = connection.execute(
            """
            SELECT id, class_id, playground_id, playground_commit_id,
                   created_by, requires_submission
            FROM releases
            """
        ).fetchall()
        self.assertEqual(
            [
                (
                    row["id"],
                    row["class_id"],
                    row["playground_id"],
                    row["playground_commit_id"],
                    row["created_by"],
                    row["requires_submission"],
                )
                for row in releases
            ],
            [("release-1", "class-1", "playground-1", "commit-1", "teacher-1", 1)],
        )
        submissions = connection.execute(
            """
            SELECT id, release_id, student_id, work_commit_id, created_at
            FROM submissions ORDER BY id
            """
        ).fetchall()
        self.assertEqual(
            [
                (
                    row["id"],
                    row["release_id"],
                    row["student_id"],
                    row["work_commit_id"],
                    row["created_at"],
                )
                for row in submissions
            ],
            [
                (
                    "submission-1",
                    "release-1",
                    "student-1",
                    "work-commit-1",
                    "2026-09-21T00:00:00Z",
                ),
                (
                    "submission-2",
                    "release-1",
                    "student-1",
                    "work-commit-2",
                    "2026-09-21T00:00:01Z",
                ),
            ],
        )

    def test_failed_comment_migration_rolls_back(self):
        self.create_version_eight_database()
        blocker = sqlite3.connect(self.database_path)
        blocker.execute(
            "CREATE TABLE comments_submission_id_created_at_idx "
            "(marker INTEGER)"
        )
        blocker.commit()
        blocker.close()

        with self.assertRaises(sqlite3.OperationalError):
            open_database(self.database_path)

        connection = sqlite3.connect(self.database_path)
        self.addCleanup(connection.close)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertNotIn("comments", tables)
        self.assertIn("comments_submission_id_created_at_idx", tables)
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7, 8])
        submissions = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM submissions ORDER BY id"
            ).fetchall()
        ]
        self.assertEqual(submissions, ["submission-1", "submission-2"])

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
