"""SQLite bootstrap for the PicoPG server."""

import sqlite3
from pathlib import Path


SCHEMA_VERSION = 2


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


def _record_migration(connection, version):
    connection.execute(
        """
        INSERT INTO schema_migrations (version, applied_at)
        VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        """,
        (version,),
    )
