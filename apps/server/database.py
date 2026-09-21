"""SQLite bootstrap for the PicoPG server."""

import re
import sqlite3
from pathlib import Path

import permissions


SCHEMA_VERSION = 2


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
    "playgrounds": {
        "id": (1, 0),
        "created_at": (0, 1),
        "state": (0, 1),
        "current_version_id": (0, 1),
    },
    "idempotency_results": {
        "scope": (1, 1),
        "endpoint": (2, 1),
        "request_key": (3, 1),
        "request_sha256": (0, 1),
        "response_status": (0, 1),
        "response_json": (0, 1),
        "created_at": (0, 1),
    },
    "events": {
        "event_id": (1, 1),
        "kind": (0, 1),
        "payload": (0, 1),
        "status": (0, 1),
        "reason": (0, 1),
        "response_json": (0, 1),
        "created_at": (0, 1),
    },
    "content_log": {
        "seq": (1, 1),
        "event_id": (0, 1),
        "kind": (0, 1),
        "payload": (0, 1),
    },
    "submissions": {
        "id": (1, 1),
        "playground_id": (0, 1),
        "playground_commit_id": (0, 1),
        "student": (0, 1),
        "files_json": (0, 1),
        "created_at": (0, 1),
    },
}

# ``(table, column, referenced table, referenced column, on-delete action)``
_FOREIGN_KEYS = (
    ("submissions", "playground_id", "playgrounds", "id", "RESTRICT"),
)

# Exact ordered column names for every index a production query relies on.
_REQUIRED_INDEXES = {
    "playgrounds_created_at_idx": ("playgrounds", ("created_at",)),
    "idempotency_results_created_at_idx": (
        "idempotency_results",
        ("created_at",),
    ),
    "events_kind_created_at_idx": ("events", ("kind", "created_at")),
    "content_log_kind_idx": ("content_log", ("kind",)),
    "submissions_playground_idx": (
        "submissions",
        ("playground_id", "created_at"),
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
    permissions.ensure_private_directory(database_path.parent)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        permissions.ensure_private_file(database_path)
        _initialize_schema(connection)
    except Exception:
        connection.close()
        raise

    return connection


def _initialize_schema(connection):
    """Create the current schema in one step.

    The model is small enough that one version describes it exactly, so there is
    no migration chain to walk.  A database from any other model carries a
    different version number and is rejected here instead of being silently
    half-migrated.
    """

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
            connection.execute(
                """
                CREATE TABLE playgrounds (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX playgrounds_created_at_idx "
                "ON playgrounds(created_at)"
            )
            connection.execute(
                """
                CREATE TABLE idempotency_results (
                    scope TEXT NOT NULL
                        CHECK (scope <> '' AND length(scope) <= 255),
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
                    PRIMARY KEY (scope, endpoint, request_key)
                )
                """
            )
            connection.execute(
                "CREATE INDEX idempotency_results_created_at_idx "
                "ON idempotency_results(created_at)"
            )
            connection.execute(
                """
                CREATE TABLE submissions (
                    id TEXT PRIMARY KEY NOT NULL,
                    playground_id TEXT NOT NULL
                        REFERENCES playgrounds(id) ON DELETE RESTRICT,
                    playground_commit_id TEXT NOT NULL
                        CHECK (playground_commit_id <> ''),
                    student TEXT NOT NULL
                        CHECK (student <> '' AND length(student) <= 128),
                    files_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX submissions_playground_idx "
                "ON submissions(playground_id, created_at)"
            )
            _record_migration(connection, 1)

        if version < 2:
            # `state` was not tracked in v1; a playground's active/stopped
            # state was implied by the existence of its Git ref. The default
            # 'active' matches the v1 behaviour for every existing row.
            connection.execute(
                "ALTER TABLE playgrounds ADD COLUMN state TEXT NOT NULL "
                "DEFAULT 'active' CHECK (state IN ('active', 'stopped'))"
            )
            # `current_version_id` was derived from Git in v1. Leave the
            # column empty for legacy rows; callers fall back to the ref
            # when the column is empty, then back-fill it on the next publish.
            connection.execute(
                "ALTER TABLE playgrounds ADD COLUMN current_version_id TEXT "
                "NOT NULL DEFAULT ''"
            )
            connection.execute(
                """
                CREATE TABLE events (
                    event_id TEXT PRIMARY KEY NOT NULL
                        CHECK (event_id <> '' AND length(event_id) <= 64),
                    kind TEXT NOT NULL
                        CHECK (kind IN ('submit_code', 'publish_playground', 'archive_playground')),
                    payload TEXT NOT NULL CHECK (payload <> ''),
                    status TEXT NOT NULL
                        CHECK (status IN ('accepted', 'rejected')),
                    reason TEXT NOT NULL DEFAULT '',
                    response_json TEXT NOT NULL CHECK (response_json <> ''),
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX events_kind_created_at_idx "
                "ON events(kind, created_at)"
            )
            connection.execute(
                """
                CREATE TABLE content_log (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE
                        REFERENCES events(event_id) ON DELETE RESTRICT,
                    kind TEXT NOT NULL
                        CHECK (kind IN ('publish_playground', 'archive_playground')),
                    payload TEXT NOT NULL CHECK (payload <> '')
                )
                """
            )
            connection.execute(
                "CREATE INDEX content_log_kind_idx "
                "ON content_log(kind)"
            )
            _record_migration(connection, 2)

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
