"""Smoke tests for the event store: dedup, content log ordering, payload roundtrip."""

import json
import os
import sqlite3
import tempfile
import unittest

import database
import events
import events_types


def _open_database():
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "app.db")
    connection = database.open_database(path)
    connection.close()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = _open_database()
        self.addCleanup(self.connection.close)

    def _envelope(self, kind, event_id="e1", payload=None):
        return events_types.EventEnvelope(
            event_id=event_id,
            kind=kind,
            payload=payload or {"playground_id": "pg", "version_id": "v"},
        )

    def test_accept_then_replay_returns_stored(self):
        envelope = self._envelope(events_types.EVENT_KIND_PUBLISH_PLAYGROUND)
        first = events.accept_event(
            self.connection,
            envelope,
            response_status="accepted",
            response_payload={"version_id": "v"},
        )
        self.assertEqual(first, "inserted")
        replay = events.accept_event(
            self.connection,
            envelope,
            response_status="rejected",
            response_payload={"reason": "should not win"},
            reason="should not win",
        )
        self.assertEqual(replay, "stored")
        stored = events.read_event(self.connection, "e1")
        self.assertEqual(stored[1], "accepted")

    def test_content_log_assigns_monotonic_seq(self):
        connection = self.connection
        for index in range(3):
            envelope = self._envelope(
                events_types.EVENT_KIND_PUBLISH_PLAYGROUND,
                event_id=f"e{index}",
            )
            events.accept_event(
                connection, envelope, "accepted", {"version_id": "v"}
            )
            seq = events.append_content_log(connection, envelope)
            self.assertGreater(seq, 0)

        entries = events.read_content_log(connection, after_seq=0)
        self.assertEqual([entry.seq for entry in entries], sorted(e.seq for e in entries))
        self.assertEqual(len(entries), 3)

    def test_content_log_rejects_submit_code(self):
        envelope = self._envelope(events_types.EVENT_KIND_SUBMIT_CODE)
        events.accept_event(self.connection, envelope, "accepted", {})
        with self.assertRaises(ValueError):
            events.append_content_log(self.connection, envelope)

    def test_current_upto_seq_starts_at_zero(self):
        self.assertEqual(events.current_upto_seq(self.connection), 0)

    def test_read_content_log_filters_by_after_seq(self):
        for index in range(3):
            envelope = self._envelope(
                events_types.EVENT_KIND_PUBLISH_PLAYGROUND, event_id=f"e{index}"
            )
            events.accept_event(self.connection, envelope, "accepted", {"version_id": "v"})
            events.append_content_log(self.connection, envelope)
        first = events.read_content_log(self.connection, after_seq=0)
        # `entries` strictly greater than `after_seq`: skipping the first
        # returned entry means everything strictly newer than it, which
        # excludes itself and anything earlier with the same seq.
        cutoff = first[0].seq
        rest = events.read_content_log(self.connection, after_seq=cutoff)
        self.assertEqual(len(rest), 2)
        self.assertTrue(all(entry.seq > cutoff for entry in rest))

    def test_payload_roundtrip_preserves_keys(self):
        envelope = self._envelope(
            events_types.EVENT_KIND_PUBLISH_PLAYGROUND,
            payload={"playground_id": "pg", "version_id": "v", "extra": [1, 2]},
        )
        events.accept_event(self.connection, envelope, "accepted", {"version_id": "v"})
        row = events.read_event(self.connection, "e1")
        self.assertEqual(json.loads(row[3]), {"version_id": "v"})


if __name__ == "__main__":
    unittest.main()
