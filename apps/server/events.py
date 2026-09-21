"""Event store: append, dedup and read.

This module owns the ``events`` and ``content_log`` tables. The events
table is the single source of truth for every event the server has
accepted: a row's primary key is the client's ``event_id``, so a replay
returns the stored ``response_json`` instead of being processed twice.

The content log is a strictly append-only side-channel that records
content events (publish and archive) in the order they were accepted.
``submit_code`` events are deliberately absent from the log — the
protocol says they belong to the client-facing ``EventResult`` stream,
not the history students see.

The store never writes partial state. Each public function runs in its
caller's transaction (``with connection:``), so the events row, the log
row, and the playground state change either all land or none of them do.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Mapping

from events_types import (
    CONTENT_EVENT_KINDS,
    EVENT_KIND_ARCHIVE_PLAYGROUND,
    EVENT_KIND_PUBLISH_PLAYGROUND,
    EVENT_KIND_SUBMIT_CODE,
    EVENT_KINDS,
    ContentLogEntry,
    EventEnvelope,
    EventResult,
)


__all__ = [
    "accept_event",
    "append_content_log",
    "current_upto_seq",
    "event_exists",
    "read_content_log",
    "read_event",
]


# --- Validation ---------------------------------------------------------

def _require_event_id(event_id):
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id must be a non-empty string")
    if len(event_id) > 64:
        raise ValueError("event_id must be at most 64 bytes")
    return event_id


def _require_kind(kind):
    if kind not in EVENT_KINDS:
        raise ValueError(f"event kind must be one of {sorted(EVENT_KINDS)}")
    return kind


def _require_payload(payload):
    if not isinstance(payload, Mapping):
        raise ValueError("event payload must be a JSON object")
    return dict(payload)


# --- Read ----------------------------------------------------------------

def event_exists(connection, event_id):
    """Whether the events table already holds this id."""
    _require_event_id(event_id)
    row = connection.execute(
        "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    return row is not None


def read_event(connection, event_id):
    """The stored row for one event id, or ``None`` when absent.

    Returns a tuple ``(kind, status, reason, response_json)``. Callers use
    the result to decide between "replay this stored response" and
    "process the event fresh".
    """
    _require_event_id(event_id)
    row = connection.execute(
        "SELECT kind, status, reason, response_json FROM events "
        "WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    return (row["kind"], row["status"], row["reason"], row["response_json"])


def read_content_log(connection, after_seq):
    """Every content log entry with ``seq > after_seq``, ordered by seq."""
    if type(after_seq) is not int or after_seq < 0:
        raise ValueError("after_seq must be a non-negative integer")
    rows = connection.execute(
        "SELECT seq, event_id, kind, payload FROM content_log "
        "WHERE seq > ? ORDER BY seq",
        (after_seq,),
    ).fetchall()
    return tuple(
        ContentLogEntry(
            seq=row["seq"],
            event=EventEnvelope(
                event_id=row["event_id"],
                kind=row["kind"],
                payload=json.loads(row["payload"]),
            ),
        )
        for row in rows
    )


def current_upto_seq(connection):
    """The seq the client should send next, or 0 if the log is empty."""
    row = connection.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM content_log").fetchone()
    return int(row["s"])


# --- Write ---------------------------------------------------------------

def accept_event(
    connection,
    envelope,
    response_status,
    response_payload,
    *,
    reason="",
):
    """Persist one accepted event together with its response body.

    The caller provides the response that should be replayed on a retry:
    ``response_payload`` is a JSON-serialisable mapping, encoded into a
    canonical string for storage. ``response_status`` is the wire status
    the client originally saw (``"accepted"`` or ``"rejected"``). Both
    fields are stored verbatim so a re-submitted event_id produces the
    exact same wire bytes.

    Returns ``"stored"`` when the event was a replay (the caller should
    hand back the stored response) and ``"inserted"`` for a fresh
    acceptance.
    """
    event_id = _require_event_id(envelope.event_id)
    kind = _require_kind(envelope.kind)
    payload = _require_payload(envelope.payload)
    if response_status not in {"accepted", "rejected"}:
        raise ValueError("response_status must be 'accepted' or 'rejected'")
    if response_status == "rejected" and not reason:
        raise ValueError("a rejected event must carry a reason")

    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    response_json = json.dumps(response_payload, separators=(",", ":"), sort_keys=True)

    try:
        with connection:
            connection.execute(
                """
                INSERT INTO events (
                    event_id, kind, payload, status, reason, response_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (event_id, kind, payload_json, response_status, reason, response_json),
            )
    except sqlite3.IntegrityError:
        return "stored"
    return "inserted"


def append_content_log(connection, envelope):
    """Add one accepted content event to the content log.

    Returns the seq assigned to the new row. Only ``publish_playground``
    and ``archive_playground`` may enter the log; ``submit_code`` is
    rejected here so a caller cannot silently mix in non-content events.
    """
    event_id = _require_event_id(envelope.event_id)
    kind = _require_kind(envelope.kind)
    if kind not in CONTENT_EVENT_KINDS:
        raise ValueError(f"content log only accepts {sorted(CONTENT_EVENT_KINDS)}")
    payload = _require_payload(envelope.payload)
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    with connection:
        cursor = connection.execute(
            "INSERT INTO content_log (event_id, kind, payload) VALUES (?, ?, ?)",
            (event_id, kind, payload_json),
        )
    return int(cursor.lastrowid)


# --- Replay helpers ------------------------------------------------------

def event_result_from_stored(stored_row):
    """Reconstruct an :class:`EventResult` from a stored row tuple."""
    _kind, status, reason, response_json = stored_row
    return EventResult(
        event_id=None,  # filled by the caller; the row tuple does not include it
        status=status,
        reason=reason,
    )


__all_event_kinds = (EVENT_KIND_SUBMIT_CODE, EVENT_KIND_PUBLISH_PLAYGROUND, EVENT_KIND_ARCHIVE_PLAYGROUND)
_ = __all_event_kinds  # silence "unused" tools
