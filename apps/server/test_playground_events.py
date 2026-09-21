"""Smoke tests for the publish/archive event helpers.

The helpers run in the caller's transaction, so every call here is
wrapped in ``with connection:`` exactly as the HTTP layer does it.
"""

import os
import sqlite3
import tempfile
import unittest

import database
import events
import events_types
from playgrounds import record_archive_event, record_publish_event


def _open_database():
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "app.db")
    connection = database.open_database(path)
    connection.close()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class PlaygroundEventTests(unittest.TestCase):
    def setUp(self):
        self.connection = _open_database()
        self.addCleanup(self.connection.close)
        with self.connection:
            self.connection.execute(
                "INSERT INTO playgrounds (id, created_at) VALUES (?, ?)",
                ("pg-1", "2026-01-01T00:00:00.000Z"),
            )

    def _publish(self, version_id):
        with self.connection:
            return record_publish_event(self.connection, "pg-1", version_id)

    def _archive(self):
        with self.connection:
            return record_archive_event(self.connection, "pg-1")

    def test_record_publish_event_writes_events_log_and_state(self):
        version_id = "a" * 40
        event_id = self._publish(version_id)
        self.assertEqual(len(event_id), 32)

        stored = events.read_event(self.connection, event_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored[0], events_types.EVENT_KIND_PUBLISH_PLAYGROUND)

        entries = events.read_content_log(self.connection, after_seq=0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].event.payload["version_id"], version_id)

        row = self.connection.execute(
            "SELECT current_version_id, state FROM playgrounds WHERE id = ?",
            ("pg-1",),
        ).fetchone()
        self.assertEqual(row["current_version_id"], version_id)
        self.assertEqual(row["state"], "active")

    def test_record_archive_event_flips_state_and_writes_log(self):
        self._publish("a" * 40)
        event_id = self._archive()
        self.assertEqual(len(event_id), 32)

        entries = events.read_content_log(self.connection, after_seq=0)
        self.assertEqual(len(entries), 2)
        self.assertEqual(
            entries[1].event.kind, events_types.EVENT_KIND_ARCHIVE_PLAYGROUND
        )

        row = self.connection.execute(
            "SELECT state FROM playgrounds WHERE id = ?", ("pg-1",)
        ).fetchone()
        self.assertEqual(row["state"], "stopped")

    def test_a_failed_caller_transaction_leaves_no_event_behind(self):
        """The helper must not commit on its own: if the caller's
        transaction fails, the event, the log row and the state flip all
        disappear together."""
        with self.assertRaises(RuntimeError):
            with self.connection:
                record_publish_event(self.connection, "pg-1", "b" * 40)
                raise RuntimeError("the rest of the operation failed")

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) AS n FROM content_log"
            ).fetchone()["n"],
            0,
        )
        row = self.connection.execute(
            "SELECT current_version_id FROM playgrounds WHERE id = ?", ("pg-1",)
        ).fetchone()
        self.assertEqual(row["current_version_id"], "")


if __name__ == "__main__":
    unittest.main()
