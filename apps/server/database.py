"""SQLite bootstrap for the PicoPG server."""

import sqlite3
from pathlib import Path


SCHEMA_VERSION = 10


def open_database(path):
    """Open and initialize a SQLite database at ``path``."""
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        _initialize_schema(connection)
    except Exception:
        connection.close()
        raise

    return connection


def _initialize_schema(connection):
    with connection:
        connection.execute("BEGIN")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                applied_at TEXT NOT NULL
            )
            """
        )

        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        version = row["version"]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {version} is newer than supported {SCHEMA_VERSION}"
            )

        if version < 1:
            _record_migration(connection, 1)

        if version < 2:
            connection.execute(
                """
                CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL
                        CHECK (role IN ('student', 'teacher', 'admin')),
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            _record_migration(connection, 2)

        if version < 3:
            connection.execute(
                """
                CREATE TABLE sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX sessions_user_id_idx ON sessions(user_id)"
            )
            _record_migration(connection, 3)

        if version < 4:
            connection.execute(
                """
                CREATE TABLE classes (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL CHECK (name <> ''),
                    teacher_id TEXT NOT NULL
                        REFERENCES users(id) ON DELETE RESTRICT,
                    created_at TEXT NOT NULL,
                    archived_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE class_memberships (
                    student_id TEXT PRIMARY KEY
                        REFERENCES users(id) ON DELETE RESTRICT,
                    class_id TEXT NOT NULL
                        REFERENCES classes(id) ON DELETE RESTRICT,
                    joined_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX classes_teacher_id_idx ON classes(teacher_id)"
            )
            connection.execute(
                "CREATE INDEX class_memberships_class_id_idx "
                "ON class_memberships(class_id)"
            )
            _record_migration(connection, 4)

        if version < 5:
            connection.execute(
                """
                CREATE TABLE playgrounds (
                    id TEXT PRIMARY KEY,
                    creator_id TEXT NOT NULL
                        REFERENCES users(id) ON DELETE RESTRICT,
                    policy TEXT NOT NULL DEFAULT 'release_only'
                        CHECK (policy IN ('release_only', 'public')),
                    created_at TEXT NOT NULL,
                    archived_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE publisher_grants (
                    user_id TEXT PRIMARY KEY
                        REFERENCES users(id) ON DELETE RESTRICT,
                    granted_by TEXT NOT NULL
                        REFERENCES users(id) ON DELETE RESTRICT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX playgrounds_creator_id_idx "
                "ON playgrounds(creator_id)"
            )
            connection.execute(
                "CREATE INDEX publisher_grants_granted_by_idx "
                "ON publisher_grants(granted_by)"
            )
            _record_migration(connection, 5)

        if version < 6:
            connection.execute(
                """
                CREATE TABLE idempotency_results (
                    actor_id TEXT NOT NULL
                        REFERENCES users(id) ON DELETE CASCADE,
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
                )
                """
            )
            _record_migration(connection, 6)

        if version < 7:
            connection.execute(
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
                )
                """
            )
            connection.execute(
                "CREATE INDEX releases_class_id_idx ON releases(class_id)"
            )
            connection.execute(
                "CREATE INDEX releases_playground_commit_id_idx "
                "ON releases(playground_id, playground_commit_id)"
            )
            _record_migration(connection, 7)

        if version < 8:
            connection.execute(
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
                )
                """
            )
            connection.execute(
                "CREATE INDEX submissions_release_id_idx "
                "ON submissions(release_id)"
            )
            connection.execute(
                "CREATE INDEX submissions_student_id_idx "
                "ON submissions(student_id)"
            )
            _record_migration(connection, 8)

        if version < 9:
            connection.execute(
                """
                CREATE TABLE comments (
                    id TEXT PRIMARY KEY NOT NULL,
                    submission_id TEXT NOT NULL
                        REFERENCES submissions(id) ON DELETE RESTRICT,
                    author_user_id TEXT
                        REFERENCES users(id) ON DELETE RESTRICT,
                    source TEXT NOT NULL
                        CHECK (source IN ('teacher', 'ai', 'system')),
                    body TEXT NOT NULL CHECK (body <> ''),
                    created_at TEXT NOT NULL,
                    CHECK (source <> 'teacher' OR author_user_id IS NOT NULL)
                )
                """
            )
            connection.execute(
                "CREATE INDEX comments_submission_id_created_at_idx "
                "ON comments(submission_id, created_at, id)"
            )
            _record_migration(connection, 9)

        if version < 10:
            connection.execute("DROP TABLE IF EXISTS publisher_grants")
            _record_migration(connection, 10)


def _record_migration(connection, version):
    connection.execute(
        """
        INSERT INTO schema_migrations (version, applied_at)
        VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        """,
        (version,),
    )
