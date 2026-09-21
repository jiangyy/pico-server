"""Smoke tests for the event envelope, content log entry, sync shapes."""

import json
import unittest

from events_types import (
    ContentLogEntry,
    EVENT_KIND_PUBLISH_PLAYGROUND,
    EventEnvelope,
    EventResult,
    PROTOCOL_VERSION,
    SyncRequest,
    SyncResponse,
)


class EventEnvelopeTests(unittest.TestCase):
    def test_to_and_from_json_roundtrip(self):
        envelope = EventEnvelope(
            event_id="e1",
            kind=EVENT_KIND_PUBLISH_PLAYGROUND,
            payload={"playground_id": "pg", "version_id": "v"},
        )
        text = envelope.to_json()
        back = EventEnvelope.from_json(text)
        self.assertEqual(back, envelope)

    def test_from_json_keeps_an_unknown_kind_structurally_valid(self):
        # protocol.md makes a kind an open string: an unknown one is a
        # well-formed event the sync engine rejects per event, not a
        # malformed request.
        envelope = EventEnvelope.from_json(
            json.dumps({"event_id": "e", "kind": "made_up", "payload": {}})
        )
        self.assertEqual(envelope.kind, "made_up")

    def test_from_json_rejects_a_missing_kind(self):
        with self.assertRaises(ValueError):
            EventEnvelope.from_json(json.dumps({"event_id": "e", "payload": {}}))

    def test_from_json_rejects_missing_payload(self):
        with self.assertRaises(ValueError):
            EventEnvelope.from_json(json.dumps({"event_id": "e", "kind": "submit_code"}))


class EventResultTests(unittest.TestCase):
    def test_accepted_result_omits_reason(self):
        result = EventResult(event_id="e", status="accepted")
        self.assertNotIn("reason", json.loads(result.to_json()))

    def test_rejected_result_carries_reason(self):
        result = EventResult(event_id="e", status="rejected", reason="playground_stopped")
        self.assertEqual(json.loads(result.to_json())["reason"], "playground_stopped")


class SyncResponseTests(unittest.TestCase):
    def test_response_carries_protocol_negotiation(self):
        response = SyncResponse(event_results=(), entries=(), upto_seq=0)
        text = response.to_json()
        body = json.loads(text)
        self.assertEqual(body["protocol_version"], PROTOCOL_VERSION)
        self.assertIn("minimum_client_protocol_version", body)
        self.assertEqual(body["upto_seq"], 0)
        self.assertEqual(body["event_results"], [])
        self.assertEqual(body["entries"], [])


class ContentLogEntryTests(unittest.TestCase):
    def test_entry_shape(self):
        envelope = EventEnvelope(
            event_id="e2",
            kind=EVENT_KIND_PUBLISH_PLAYGROUND,
            payload={"playground_id": "pg", "version_id": "v"},
        )
        entry = ContentLogEntry(seq=7, event=envelope)
        self.assertEqual(entry.seq, 7)
        self.assertEqual(entry.event.event_id, "e2")


class SyncRequestTests(unittest.TestCase):
    def test_request_round_trip_through_stringification(self):
        envelope = EventEnvelope(
            event_id="e",
            kind="submit_code",
            payload={"playground_id": "p", "version_id": "v", "files": {}},
        )
        request = SyncRequest(pending_events=(envelope,), after_seq=3)
        self.assertEqual(request.after_seq, 3)
        self.assertEqual(len(request.pending_events), 1)


if __name__ == "__main__":
    unittest.main()
