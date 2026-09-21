"""The sync engine: process one ``SyncRequest`` end to end.

A request carries:

  - ``pending_events``: events the client has produced and wants
    acknowledged.
  - ``after_seq``: the client's last seen content cursor.

The engine processes pending events in the order they were submitted.
Each one is either replayed (the ``event_id`` already exists in the
events table, so the stored result is returned unchanged) or accepted
through a per-kind handler. The handler decides whether to write a
content log entry, update playground state, or both. After all pending
events are settled, the engine reads every content log entry whose seq
is greater than ``after_seq`` and bundles them with the matched
results and the new ``upto_seq``.

The engine never writes on its own: the per-kind handlers are the only
callers of ``events.accept_event`` and ``events.append_content_log``,
and they run inside the same transaction the engine opens. A handler
that raises aborts the whole round — no event is half-accepted.
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Mapping

import events
import events_types


# A handler returns the JSON body to store as the event's response.
# Raising rejects the event: the engine records the rejection with the
# raised value's ``reason`` attribute.
class EventRejected(Exception):
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

    ``submit_handler``, ``publish_handler`` and ``archive_handler`` are
    the per-kind handlers. Each is called with ``(connection,
    envelope)`` and returns the JSON body that should be replayed on a
    retry. A handler that raises :class:`EventRejected` produces a
    rejected ``EventResult`` for that event without affecting the rest
    of the round.
    """
    handlers = {
        events_types.EVENT_KIND_SUBMIT_CODE: submit_handler,
        events_types.EVENT_KIND_PUBLISH_PLAYGROUND: publish_handler,
        events_types.EVENT_KIND_ARCHIVE_PLAYGROUND: archive_handler,
    }

    event_results = []
    inserted_events = []

    with connection:  # one transaction for the whole round
        for envelope in request.pending_events:
            stored = events.read_event(connection, envelope.event_id)
            if stored is not None:
                _kind, status, reason, response_json = stored
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
            inserted_events.append(envelope)

    entries = events.read_content_log(connection, request.after_seq)
    upto_seq = events.current_upto_seq(connection)

    return events_types.SyncResponse(
        event_results=tuple(event_results),
        entries=entries,
        upto_seq=upto_seq,
    )
