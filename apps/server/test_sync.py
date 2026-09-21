"""Smoke tests for the sync engine."""

import json
import os
import sqlite3
import tempfile
import unittest

import database
import events_types
import sync


def _open_database():
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "app.db")
    connection = database.open_database(path)
    connection.close()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class SyncEngineTests(unittest.TestCase):
    def setUp(self):
        self.connection = _open_database()
        self.addCleanup(self.connection.close)

    def test_process_sync_replays_stored_events(self):
        envelope = events_types.EventEnvelope(
            event_id="e1",
            kind=events_types.EVENT_KIND_PUBLISH_PLAYGROUND,
            payload={"playground_id": "pg", "version_id": "v"},
        )
        first = sync.process_sync(
            self.connection,
            events_types.SyncRequest(pending_events=(envelope,), after_seq=0),
            submit_handler=_rejecting(events_types.REASON_PLAYGROUND_NOT_FOUND),
            publish_handler=lambda _connection, _env: {"version_id": "v"},
            archive_handler=_rejecting(events_types.REASON_UNKNOWN_KIND),
        )
        self.assertEqual(first.event_results[0].status, "accepted")

        # A second round with the same event_id must replay, not re-run
        # the handler.
        calls = {"publish": 0}

        def counting(_connection, _env):
            calls["publish"] += 1
            return {"version_id": "v"}

        second = sync.process_sync(
            self.connection,
            events_types.SyncRequest(pending_events=(envelope,), after_seq=1),
            submit_handler=_rejecting(events_types.REASON_INVALID_SUBMISSION),
            publish_handler=counting,
            archive_handler=_rejecting(events_types.REASON_UNKNOWN_KIND),
        )
        self.assertEqual(second.event_results[0].status, "accepted")
        self.assertEqual(calls["publish"], 0)

    def test_process_sync_rejects_unknown_kind(self):
        envelope = events_types.EventEnvelope(
            event_id="e2",
            kind="made_up",
            payload={},
        )
        response = sync.process_sync(
            self.connection,
            events_types.SyncRequest(pending_events=(envelope,), after_seq=0),
            submit_handler=_rejecting(events_types.REASON_INVALID_SUBMISSION),
            publish_handler=_rejecting(events_types.REASON_PLAYGROUND_STOPPED),
            archive_handler=_rejecting(events_types.REASON_UNKNOWN_KIND),
        )
        result = response.event_results[0]
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, events_types.REASON_UNKNOWN_KIND)

    def test_process_sync_records_handler_rejection(self):
        envelope = events_types.EventEnvelope(
            event_id="e3",
            kind=events_types.EVENT_KIND_SUBMIT_CODE,
            payload={"playground_id": "pg", "version_id": "v", "files": {}},
        )
        response = sync.process_sync(
            self.connection,
            events_types.SyncRequest(pending_events=(envelope,), after_seq=0),
            submit_handler=_rejecting(events_types.REASON_PLAYGROUND_STOPPED),
            publish_handler=_rejecting(events_types.REASON_PLAYGROUND_STOPPED),
            archive_handler=_rejecting(events_types.REASON_UNKNOWN_KIND),
        )
        result = response.event_results[0]
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.reason, events_types.REASON_PLAYGROUND_STOPPED)

        # A retry with the same event_id replays the stored rejection.
        retry = sync.process_sync(
            self.connection,
            events_types.SyncRequest(pending_events=(envelope,), after_seq=0),
            submit_handler=lambda *_args, **_kwargs: {"ok": True},
            publish_handler=_rejecting(events_types.REASON_PLAYGROUND_STOPPED),
            archive_handler=_rejecting(events_types.REASON_UNKNOWN_KIND),
        )
        self.assertEqual(retry.event_results[0].status, "rejected")

    def test_process_sync_emits_content_log_entries_for_publish(self):
        envelope = events_types.EventEnvelope(
            event_id="e4",
            kind=events_types.EVENT_KIND_PUBLISH_PLAYGROUND,
            payload={"playground_id": "pg", "version_id": "v"},
        )
        response = sync.process_sync(
            self.connection,
            events_types.SyncRequest(pending_events=(envelope,), after_seq=0),
            submit_handler=_rejecting(events_types.REASON_INVALID_SUBMISSION),
            publish_handler=lambda _connection, _env: {"version_id": "v"},
            archive_handler=_rejecting(events_types.REASON_UNKNOWN_KIND),
        )
        self.assertEqual(len(response.entries), 1)
        self.assertEqual(response.entries[0].event.event_id, "e4")
        self.assertEqual(response.upto_seq, response.entries[0].seq)

    def test_an_unexpected_handler_failure_rolls_the_whole_round_back(self):
        """One round is one transaction: an accepted first event and a
        hard failure on the second must leave no trace of either."""

        def boom(_connection, _envelope):
            raise RuntimeError("unexpected")

        first = events_types.EventEnvelope(
            event_id="e-first",
            kind=events_types.EVENT_KIND_PUBLISH_PLAYGROUND,
            payload={"playground_id": "pg", "version_id": "v"},
        )
        second = events_types.EventEnvelope(
            event_id="e-second",
            kind=events_types.EVENT_KIND_PUBLISH_PLAYGROUND,
            payload={"playground_id": "pg", "version_id": "w"},
        )
        with self.assertRaises(RuntimeError):
            sync.process_sync(
                self.connection,
                events_types.SyncRequest(pending_events=(first, second), after_seq=0),
                submit_handler=_rejecting(events_types.REASON_INVALID_SUBMISSION),
                publish_handler=boom,
                archive_handler=_rejecting(events_types.REASON_UNKNOWN_KIND),
            )

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


def _rejecting(reason):
    def handler(_connection, _envelope):
        raise sync.EventRejected(reason)

    return handler


if __name__ == "__main__":
    unittest.main()
