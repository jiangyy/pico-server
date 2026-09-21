"""Domain types for the server's event log.

Source of truth: `learn-python/apps/client/protocol.md`. Every wire field is
named exactly as the protocol specifies, so a JSON body decodes straight
into a value here with no translation layer. The types are deliberately
flat — one shape per concept — and the storage module turns them into
SQLite rows without changing their names.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


# --- Protocol negotiation -------------------------------------------------

#: The protocol version this server speaks. Bumped in lockstep with the
#: client when the wire shape changes.
PROTOCOL_VERSION = 1

#: The oldest protocol version a client is still allowed to speak. A
#: client older than this must be rejected (or refused the sync channel)
#: rather than silently mis-interpreted.
MINIMUM_CLIENT_PROTOCOL_VERSION = 1

#: Event kinds the protocol defines. Anything else is rejected at the wire.
EVENT_KIND_SUBMIT_CODE = "submit_code"
EVENT_KIND_PUBLISH_PLAYGROUND = "publish_playground"
EVENT_KIND_ARCHIVE_PLAYGROUND = "archive_playground"
EVENT_KINDS = frozenset(
    {EVENT_KIND_SUBMIT_CODE, EVENT_KIND_PUBLISH_PLAYGROUND, EVENT_KIND_ARCHIVE_PLAYGROUND}
)

#: Content event kinds go into the content log; submit_code does not.
CONTENT_EVENT_KINDS = frozenset(
    {EVENT_KIND_PUBLISH_PLAYGROUND, EVENT_KIND_ARCHIVE_PLAYGROUND}
)


# --- Event envelopes ------------------------------------------------------

@dataclass(frozen=True)
class EventEnvelope:
    """One event as it appears on the wire and in the events table."""

    event_id: str
    kind: str
    payload: Mapping[str, Any]

    def to_json(self) -> str:
        return json.dumps(
            {"event_id": self.event_id, "kind": self.kind, "payload": dict(self.payload)},
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def from_mapping(value: Any) -> "EventEnvelope":
        """Build an envelope from a decoded JSON value.

        Structural validation only: an ``event_id``, a ``kind`` and an object
        ``payload`` are required. The ``kind`` is deliberately **not** checked
        against a closed set — protocol.md says a kind is an open string, so an
        unknown one is a well-formed event the sync engine rejects per event
        (``unknown_kind``) rather than a malformed request.
        """
        if not isinstance(value, dict):
            raise ValueError("event envelope is not a JSON object")
        event_id = value.get("event_id")
        kind = value.get("kind")
        payload = value.get("payload")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id is missing or not a non-empty string")
        if not isinstance(kind, str) or not kind:
            raise ValueError("event kind is missing or not a non-empty string")
        if not isinstance(payload, dict):
            raise ValueError("event payload is not a JSON object")
        return EventEnvelope(event_id=event_id, kind=kind, payload=payload)

    @staticmethod
    def from_json(text: str) -> "EventEnvelope":
        return EventEnvelope.from_mapping(json.loads(text))


@dataclass(frozen=True)
class ContentLogEntry:
    """One row of the content log: a published or archived event with its seq."""

    seq: int
    event: EventEnvelope


@dataclass(frozen=True)
class EventResult:
    """The server's reply for one event the client submitted."""

    event_id: str
    status: str  # "accepted" or "rejected"
    reason: str = ""

    def to_json(self) -> str:
        body: dict[str, Any] = {"event_id": self.event_id, "status": self.status}
        if self.status == "rejected":
            body["reason"] = self.reason
        return json.dumps(body, separators=(",", ":"), sort_keys=True)


# --- Sync -----------------------------------------------------------------

@dataclass(frozen=True)
class SyncRequest:
    pending_events: tuple[EventEnvelope, ...]
    after_seq: int


@dataclass(frozen=True)
class SyncResponse:
    event_results: tuple[EventResult, ...]
    entries: tuple[ContentLogEntry, ...]
    upto_seq: int
    protocol_version: int = PROTOCOL_VERSION
    minimum_client_protocol_version: int = MINIMUM_CLIENT_PROTOCOL_VERSION

    def to_json(self) -> str:
        return json.dumps(
            {
                "event_results": [
                    json.loads(result.to_json()) for result in self.event_results
                ],
                "entries": [
                    {
                        "seq": entry.seq,
                        "event": {
                            "event_id": entry.event.event_id,
                            "kind": entry.event.kind,
                            "payload": dict(entry.event.payload),
                        },
                    }
                    for entry in self.entries
                ],
                "upto_seq": self.upto_seq,
                "protocol_version": self.protocol_version,
                "minimum_client_protocol_version": self.minimum_client_protocol_version,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


# --- Result reason codes --------------------------------------------------

REASON_PLAYGROUND_STOPPED = "playground_stopped"
REASON_INVALID_VERSION = "invalid_version"
REASON_INVALID_SUBMISSION = "invalid_submission"
# Reserved by protocol.md: an event id reused with different content. Nothing
# produces it — an event id is replayed from storage whatever its payload, which
# is the whole point of the id — so it stays named rather than re-invented.
REASON_EVENT_CONFLICT = "event_conflict"
REASON_PLAYGROUND_NOT_FOUND = "playground_not_found"
REASON_INVALID_EVENT = "invalid_event"
REASON_UNKNOWN_KIND = "unknown_kind"
