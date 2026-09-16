"""SQLite bootstrap for the PicoPG server."""

import re
import sqlite3
from pathlib import Path


SCHEMA_VERSION = 10


class SchemaValidationError(RuntimeError):
    """A database does not have the exact schema this Server requires.

    ``category`` is one of ``migration``, ``table``, ``column``, ``index`` or
    ``foreign_key``.  The message is the category itself so no row content,
    credential, or path can leak through an error string.
    """

    CATEGORIES = frozenset(
        {"migration", "table", "column", "index", "foreign_key"}
    )

    def __init__(self, category):
        if category not in self.CATEGORIES:
            raise ValueError("unknown schema validation category")
        self.category = category
        super().__init__(category)


# --- Declarative current-schema contract ---------------------------------
#
# Each required table maps a column name to ``(primary-key position, notnull)``.
# A primary-key position of ``0`` marks a non-key column.  These values are
# exactly what ``PRAGMA table_info`` reports for the current migration set, so
# a missing column, a changed key shape, or a wrong nullability is detected.
_TABLE_COLUMNS = {
    "schema_migrations": {
        "version": (1, 0),
        "applied_at": (0, 1),
    },
    "users": {
        "id": (1, 0),
        "username": (0, 1),
        "role": (0, 1),
        "password_hash": (0, 1),
        "created_at": (0, 1),
    },
    "sessions": {
        "token_hash": (1, 0),
        "user_id": (0, 1),
        "created_at": (0, 1),
        "expires_at": (0, 1),
        "revoked_at": (0, 0),
    },
    "classes": {
        "id": (1, 0),
        "name": (0, 1),
        "teacher_id": (0, 1),
        "created_at": (0, 1),
        "archived_at": (0, 0),
    },
    "class_memberships": {
        "student_id": (1, 0),
        "class_id": (0, 1),
        "joined_at": (0, 1),
    },
    "playgrounds": {
        "id": (1, 0),
        "creator_id": (0, 1),
        "policy": (0, 1),
        "created_at": (0, 1),
        "archived_at": (0, 0),
    },
    "idempotency_results": {
        "actor_id": (1, 1),
        "endpoint": (2, 1),
        "request_key": (3, 1),
        "request_sha256": (0, 1),
        "response_status": (0, 1),
        "response_json": (0, 1),
        "created_at": (0, 1),
    },
    "releases": {
        "id": (1, 1),
        "class_id": (0, 1),
        "playground_id": (0, 1),
        "playground_commit_id": (0, 1),
        "created_by": (0, 1),
        "open_at": (0, 1),
        "due_at": (0, 0),
        "requires_submission": (0, 1),
        "created_at": (0, 1),
    },
    "submissions": {
        "id": (1, 1),
        "release_id": (0, 1),
        "student_id": (0, 1),
        "work_commit_id": (0, 1),
        "created_at": (0, 1),
    },
    "comments": {
        "id": (1, 1),
        "submission_id": (0, 1),
        "author_user_id": (0, 0),
        "source": (0, 1),
        "body": (0, 1),
        "created_at": (0, 1),
    },
}

# A table dropped by migration 10 must not reappear at the current version.
_FORBIDDEN_TABLES = frozenset({"publisher_grants"})

# ``(table, column, referenced table, referenced column, on-delete action)``
_FOREIGN_KEYS = (
    ("sessions", "user_id", "users", "id", "CASCADE"),
    ("classes", "teacher_id", "users", "id", "RESTRICT"),
    ("class_memberships", "student_id", "users", "id", "RESTRICT"),
    ("class_memberships", "class_id", "classes", "id", "RESTRICT"),
    ("playgrounds", "creator_id", "users", "id", "RESTRICT"),
    ("idempotency_results", "actor_id", "users", "id", "CASCADE"),
    ("releases", "class_id", "classes", "id", "RESTRICT"),
    ("releases", "playground_id", "playgrounds", "id", "RESTRICT"),
    ("releases", "created_by", "users", "id", "RESTRICT"),
    ("submissions", "release_id", "releases", "id", "RESTRICT"),
    ("submissions", "student_id", "users", "id", "RESTRICT"),
    ("comments", "submission_id", "submissions", "id", "RESTRICT"),
    ("comments", "author_user_id", "users", "id", "RESTRICT"),
)

# Exact ordered column names for every index a production query relies on.
_REQUIRED_INDEXES = {
    "sessions_user_id_idx": ("sessions", ("user_id",)),
    "classes_teacher_id_idx": ("classes", ("teacher_id",)),
    "class_memberships_class_id_idx": (
        "class_memberships",
        ("class_id",),
    ),
    "playgrounds_creator_id_idx": ("playgrounds", ("creator_id",)),
    "releases_class_id_idx": ("releases", ("class_id",)),
    "releases_playground_commit_id_idx": (
        "releases",
        ("playground_id", "playground_commit_id"),
    ),
    "submissions_release_id_idx": ("submissions", ("release_id",)),
    "submissions_student_id_idx": ("submissions", ("student_id",)),
    "comments_submission_id_created_at_idx": (
        "comments",
        ("submission_id", "created_at", "id"),
    ),
}

_SQLITE_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def _pragma_identifier(value):
    if _SQLITE_IDENTIFIER.match(value) is None:
        raise SchemaValidationError("table")
    return f'"{value}"'


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
            raise SchemaValidationError("migration")

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

        validate_schema(connection)


def _record_migration(connection, version):
    connection.execute(
        """
        INSERT INTO schema_migrations (version, applied_at)
        VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        """,
        (version,),
    )


def validate_schema(connection):
    """Validate the complete current schema on a read-only connection.

    Issues no writes.  Raises :class:`SchemaValidationError` naming only the
    failing category.  Extra user-created indexes are allowed; a missing or
    incompatible required table, column, index, foreign key, or migration
    history is not.
    """

    tables = _table_names(connection)
    _validate_migrations(connection, tables)
    for table in _FORBIDDEN_TABLES:
        if table in tables:
            raise SchemaValidationError("table")
    for table, columns in _TABLE_COLUMNS.items():
        if table not in tables:
            raise SchemaValidationError("table")
        _validate_columns(connection, table, columns)
    _validate_foreign_keys(connection)
    _validate_indexes(connection)


def _table_names(connection):
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    except sqlite3.Error as error:
        raise SchemaValidationError("table") from error
    return {row[0] for row in rows}


def _validate_migrations(connection, tables):
    if "schema_migrations" not in tables:
        raise SchemaValidationError("migration")
    try:
        rows = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.Error as error:
        raise SchemaValidationError("migration") from error
    observed = []
    for row in rows:
        value = row[0]
        # Exact integer comparison: a text, float, or NULL version is not a
        # valid migration record.
        if type(value) is not int:
            raise SchemaValidationError("migration")
        observed.append(value)
    if observed != list(range(1, SCHEMA_VERSION + 1)):
        raise SchemaValidationError("migration")


def _validate_columns(connection, table, expected):
    try:
        rows = connection.execute(
            f"PRAGMA table_info({_pragma_identifier(table)})"
        ).fetchall()
    except sqlite3.Error as error:
        raise SchemaValidationError("column") from error
    if not rows:
        raise SchemaValidationError("table")

    actual = {}
    for row in rows:
        name = row[1]
        if type(name) is not str or name not in expected:
            raise SchemaValidationError("column")
        actual[name] = (row[3], row[5])

    if set(actual) != set(expected):
        raise SchemaValidationError("column")

    for name, (expected_pk, expected_notnull) in expected.items():
        notnull, pk = actual[name]
        if pk != expected_pk:
            raise SchemaValidationError("column")
        # INTEGER PRIMARY KEY columns are rowid aliases and report
        # ``notnull = 0`` even though they are never NULL; only non-key
        # columns carry a meaningful nullability contract here.
        if expected_pk == 0 and notnull != expected_notnull:
            raise SchemaValidationError("column")


def _validate_foreign_keys(connection):
    for (
        table,
        column,
        ref_table,
        ref_column,
        on_delete,
    ) in _FOREIGN_KEYS:
        try:
            rows = connection.execute(
                f"PRAGMA foreign_key_list({_pragma_identifier(table)})"
            ).fetchall()
        except sqlite3.Error as error:
            raise SchemaValidationError("foreign_key") from error
        found = any(
            row[3] == column
            and row[2] == ref_table
            and row[4] == ref_column
            and row[6] == on_delete
            for row in rows
        )
        if not found:
            raise SchemaValidationError("foreign_key")


def _validate_indexes(connection):
    for name, (table, columns) in _REQUIRED_INDEXES.items():
        try:
            listed = {
                row[1]
                for row in connection.execute(
                    f"PRAGMA index_list({_pragma_identifier(table)})"
                ).fetchall()
            }
            if name not in listed:
                raise SchemaValidationError("index")
            info = connection.execute(
                f"PRAGMA index_info({_pragma_identifier(name)})"
            ).fetchall()
        except sqlite3.Error as error:
            raise SchemaValidationError("index") from error
        observed = tuple(row[2] for row in info)
        if observed != columns:
            raise SchemaValidationError("index")
