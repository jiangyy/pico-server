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

This module never manages transactions. Every function runs inside the
caller's open transaction; the caller is the only one that opens
(``with connection:``) and commits, so a whole sync round is one unit:
either every event row, log row and Playground state change lands, or
none of them do. Python's ``sqlite3`` ``with connection:`` is a
commit-on-exit, not a savepoint, so a nested one inside an outer
transaction would commit the outer work early — there is deliberately
none here.
"""

from __future__ import annotations

import json
from typing import Mapping

from events_types import (
    CONTENT_EVENT_KINDS,
    EVENT_KINDS,
    ContentLogEntry,
    EventEnvelope,
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


def read_content_log(connection, after_seq, limit=None):
    """Content log entries with ``seq > after_seq``, oldest first.

    ``limit`` bounds one response. The log grows for the life of the
    deployment, so an unbounded read would eventually produce a body larger
    than a client is willing to accept — and that client could never advance
    its cursor again. A caller that bounds the read must report the seq it
    actually delivered, never the log's global maximum.
    """
    if type(after_seq) is not int or after_seq < 0:
        raise ValueError("after_seq must be a non-negative integer")
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("limit must be a positive integer or None")
    query = (
        "SELECT seq, event_id, kind, payload FROM content_log "
        "WHERE seq > ? ORDER BY seq"
    )
    parameters = [after_seq]
    if limit is not None:
        query += " LIMIT ?"
        parameters.append(limit)
    rows = connection.execute(query, parameters).fetchall()
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

    Runs in the caller's transaction and never commits. The caller checks
    :func:`read_event` first, so a duplicate id here means a genuine
    race: the ``IntegrityError`` propagates and rolls the caller's whole
    transaction back, which is what makes the retry return the winner's
    stored result instead of a half-applied round.
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

    connection.execute(
        """
        INSERT INTO events (
            event_id, kind, payload, status, reason, response_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        """,
        (event_id, kind, payload_json, response_status, reason, response_json),
    )


def append_content_log(connection, envelope):
    """Add one accepted content event to the content log.

    Returns the seq assigned to the new row. Only ``publish_playground``
    and ``archive_playground`` may enter the log; ``submit_code`` is
    rejected here so a caller cannot silently mix in non-content events.

    Runs in the caller's transaction and never commits.
    """
    event_id = _require_event_id(envelope.event_id)
    kind = _require_kind(envelope.kind)
    if kind not in CONTENT_EVENT_KINDS:
        raise ValueError(f"content log only accepts {sorted(CONTENT_EVENT_KINDS)}")
    payload = _require_payload(envelope.payload)
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    cursor = connection.execute(
        "INSERT INTO content_log (event_id, kind, payload) VALUES (?, ?, ?)",
        (event_id, kind, payload_json),
    )
    return int(cursor.lastrowid)
