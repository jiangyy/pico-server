"""The sync engine: process one ``SyncRequest`` end to end.

A request carries:

  - ``pending_events``: events the client has produced and wants
    acknowledged.
  - ``after_seq``: the client's last seen content cursor.

The engine processes pending events in the order they were submitted.
Each one is either replayed (the ``event_id`` already exists in the
events table, so the stored result is returned unchanged) or handled by
the per-kind handler bound to that kind. After all pending events are
settled, the engine reads every content log entry whose seq is greater
than ``after_seq`` and bundles them with the matched results and the new
``upto_seq``.

The engine owns the round's transaction and delegates every write to the
handlers, which run inside it. A handler that raises :class:`EventRejected`
records a rejection and moves on; any other exception aborts the whole
round, so no event is half-applied.

An unknown ``kind`` is not an error. protocol.md makes a kind an open
string, so an event this server does not understand is rejected per event
(``unknown_kind``) and the rest of the round still runs. The rejection is
not persisted: it is a deterministic answer to a request this server will
never interpret, so a retry recomputes it from the same bytes.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

import database
import events
import events_types


#: How many content log entries one sync response carries. The client reads a
#: response body under a 1 MiB cap, so an unbounded log read would eventually
#: answer with a body the client refuses — leaving it unable to advance.
MAX_ENTRIES_PER_SYNC = 500


class EventRejected(Exception):
    """A handler's refusal of one event, carrying a stable reason code."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


Handler = Callable[[sqlite3.Connection, events_types.EventEnvelope], dict]


def process_sync(
    connection,
    request,
    *,
    submit_handler,
    publish_handler,
    archive_handler,
):
    """Process one sync round and return a :class:`SyncResponse`.

    Each handler is called with ``(connection, envelope)`` and returns the
    JSON body to replay on a retry. Raising :class:`EventRejected` rejects
    that one event with the raised reason.
    """
    handlers = {
        events_types.EVENT_KIND_SUBMIT_CODE: submit_handler,
        events_types.EVENT_KIND_PUBLISH_PLAYGROUND: publish_handler,
        events_types.EVENT_KIND_ARCHIVE_PLAYGROUND: archive_handler,
    }

    event_results = []

    # One immediate transaction for the whole round, so the "has this
    # event been seen?" read and the insert that follows it cannot be
    # interleaved with another connection doing the same check.
    with database.immediate_transaction(connection):
        for envelope in request.pending_events:
            stored = events.read_event(connection, envelope.event_id)
            if stored is not None:
                _kind, status, reason, _response_json = stored
                event_results.append(
                    events_types.EventResult(
                        event_id=envelope.event_id,
                        status=status,
                        reason=reason,
                    )
                )
                continue

            handler = handlers.get(envelope.kind)
            if handler is None:
                event_results.append(
                    events_types.EventResult(
                        event_id=envelope.event_id,
                        status="rejected",
                        reason=events_types.REASON_UNKNOWN_KIND,
                    )
                )
                continue

            try:
                response_body = handler(connection, envelope)
            except EventRejected as rejection:
                events.accept_event(
                    connection,
                    envelope,
                    response_status="rejected",
                    response_payload={"reason": rejection.reason},
                    reason=rejection.reason,
                )
                event_results.append(
                    events_types.EventResult(
                        event_id=envelope.event_id,
                        status="rejected",
                        reason=rejection.reason,
                    )
                )
                continue

            events.accept_event(
                connection,
                envelope,
                response_status="accepted",
                response_payload=response_body,
            )
            if envelope.kind in events_types.CONTENT_EVENT_KINDS:
                events.append_content_log(connection, envelope)
            event_results.append(
                events_types.EventResult(
                    event_id=envelope.event_id,
                    status="accepted",
                )
            )

    entries = events.read_content_log(
        connection, request.after_seq, MAX_ENTRIES_PER_SYNC
    )
    # The cursor is the last seq *delivered*, not the log's maximum: advancing
    # past entries this response did not carry would lose them for good.
    upto_seq = entries[-1].seq if entries else request.after_seq

    return events_types.SyncResponse(
        event_results=tuple(event_results),
        entries=entries,
        upto_seq=upto_seq,
    )
