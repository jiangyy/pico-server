"""Smoke tests for the event store: dedup, content log ordering, payload roundtrip.

The store never manages transactions, so every write here is wrapped in
``with connection:`` exactly as the real callers do. That is part of the
contract under test: a write that is not inside a caller transaction is
never committed.
"""

import json
import os
import sqlite3
import tempfile
import unittest

import database
import events
import events_types


def _open_database(path=None):
    if path is None:
        path = os.path.join(tempfile.mkdtemp(), "app.db")
        database.open_database(path).close()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "app.db")
        database.open_database(self.path).close()
        self.connection = _open_database(self.path)
        self.addCleanup(self.connection.close)

    def _envelope(self, kind, event_id="e1", payload=None):
        return events_types.EventEnvelope(
            event_id=event_id,
            kind=kind,
            payload=payload or {"playground_id": "pg", "version_id": "v"},
        )

    def _accept(self, envelope, status="accepted", body=None, reason=""):
        with self.connection:
            events.accept_event(
                self.connection,
                envelope,
                response_status=status,
                response_payload=body if body is not None else {"version_id": "v"},
                reason=reason,
            )

    def test_read_event_reports_a_stored_event(self):
        envelope = self._envelope(events_types.EVENT_KIND_PUBLISH_PLAYGROUND)
        self.assertIsNone(events.read_event(self.connection, "e1"))
        self._accept(envelope)
        stored = events.read_event(self.connection, "e1")
        self.assertEqual(stored[0], events_types.EVENT_KIND_PUBLISH_PLAYGROUND)
        self.assertEqual(stored[1], "accepted")
        self.assertEqual(json.loads(stored[3]), {"version_id": "v"})

    def test_a_duplicate_event_id_raises_and_leaves_the_first_row(self):
        envelope = self._envelope(events_types.EVENT_KIND_PUBLISH_PLAYGROUND)
        self._accept(envelope)
        # The caller checks read_event first; a duplicate here is a genuine
        # race, and the IntegrityError is what rolls the round back.
        with self.assertRaises(sqlite3.IntegrityError):
            with self.connection:
                events.accept_event(
                    self.connection,
                    envelope,
                    response_status="rejected",
                    response_payload={"reason": "should not win"},
                    reason="should not win",
                )
        stored = events.read_event(self.connection, "e1")
        self.assertEqual(stored[1], "accepted")

    def test_content_log_assigns_monotonic_seq(self):
        connection = self.connection
        for index in range(3):
            envelope = self._envelope(
                events_types.EVENT_KIND_PUBLISH_PLAYGROUND,
                event_id=f"e{index}",
            )
            with connection:
                events.accept_event(
                    connection, envelope, "accepted", {"version_id": "v"}
                )
                seq = events.append_content_log(connection, envelope)
            self.assertGreater(seq, 0)

        entries = events.read_content_log(connection, after_seq=0)
        self.assertEqual(
            [entry.seq for entry in entries], sorted(e.seq for e in entries)
        )
        self.assertEqual(len(entries), 3)

    def test_content_log_rejects_submit_code(self):
        envelope = self._envelope(events_types.EVENT_KIND_SUBMIT_CODE)
        self._accept(envelope, body={})
        with self.assertRaises(ValueError):
            events.append_content_log(self.connection, envelope)

    def test_current_upto_seq_starts_at_zero(self):
        self.assertEqual(events.current_upto_seq(self.connection), 0)

    def test_read_content_log_filters_by_after_seq(self):
        for index in range(3):
            envelope = self._envelope(
                events_types.EVENT_KIND_PUBLISH_PLAYGROUND, event_id=f"e{index}"
            )
            with self.connection:
                events.accept_event(
                    self.connection, envelope, "accepted", {"version_id": "v"}
                )
                events.append_content_log(self.connection, envelope)
        first = events.read_content_log(self.connection, after_seq=0)
        # `entries` strictly greater than `after_seq`: skipping the first
        # returned entry means everything strictly newer than it, which
        # excludes itself and anything earlier with the same seq.
        cutoff = first[0].seq
        rest = events.read_content_log(self.connection, after_seq=cutoff)
        self.assertEqual(len(rest), 2)
        self.assertTrue(all(entry.seq > cutoff for entry in rest))

    def test_an_uncommitted_write_is_not_visible_to_another_connection(self):
        """The store has no transaction of its own: without a caller's
        ``with connection:`` nothing is ever committed."""
        envelope = self._envelope(events_types.EVENT_KIND_PUBLISH_PLAYGROUND)
        events.accept_event(
            self.connection,
            envelope,
            response_status="accepted",
            response_payload={"version_id": "v"},
        )
        observer = _open_database(self.path)
        self.addCleanup(observer.close)
        self.assertIsNone(events.read_event(observer, "e1"))


if __name__ == "__main__":
    unittest.main()
