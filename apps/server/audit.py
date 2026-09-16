"""Bounded, structured audit events for the PicoPG HTTP service.

The service records one JSON object per line for authenticated mutations and
for authorization failures.  Values are sanitized and bounded before they are
encoded, and no request body, credential, or source data is ever accepted by
this module.  Every external effect (output stream, clock, request-ID source)
is injectable so tests can capture and assert the exact event stream.
"""

import json
import re
import sys
import threading
import uuid
from datetime import datetime, timezone


# A client-supplied request ID is only echoed when it is short and made of
# header-safe ASCII.  Anything else is replaced by a generated ID.
MAX_REQUEST_ID_LENGTH = 64
MAX_ACTION_LENGTH = 64
MAX_FIELD_LENGTH = 128

_REQUEST_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_CONTROL_TABLE = str.maketrans(
    {code: "?" for code in range(0x20)} | {0x7F: "?"}
)


def sanitize(value, limit=MAX_FIELD_LENGTH):
    """Return ``value`` with control characters replaced and length bounded.

    ``None`` is preserved so optional fields stay JSON ``null`` rather than
    becoming the string ``"None"``.
    """

    if value is None:
        return None
    text = str(value).translate(_CONTROL_TABLE)
    if len(text) > limit:
        text = text[:limit]
    return text


def _format_time(moment):
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _duration_ms(started_at, finished_at):
    if started_at is None or finished_at is None:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=timezone.utc)
    elapsed = (finished_at - started_at).total_seconds() * 1000
    if elapsed < 0:
        elapsed = 0.0
    return round(elapsed, 3)


class AuditLog:
    """Emit bounded JSON-line audit events to one output stream.

    ``output`` receives completed lines, ``clock`` returns aware datetimes, and
    ``request_ids`` returns a candidate ID for each request that did not supply
    an acceptable one.  Defaults use standard error, the current UTC time, and
    ``uuid4``; tests replace all three.
    """

    def __init__(self, output=None, clock=None, request_ids=None):
        self._output = output if output is not None else sys.stderr
        self._clock = clock if clock is not None else _utcnow
        self._request_ids = (
            request_ids if request_ids is not None else _new_uuid
        )
        self._lock = threading.Lock()

    def now(self):
        """Return the current instant from the injected clock."""

        return self._clock()

    def request_id(self, supplied=None, secret=None):
        """Return an accepted client ID, or a bounded generated replacement.

        ``secret`` is the request's bearer/session token when one was
        supplied.  An accepted request ID uses the same header-safe alphabet
        as a generated session token, so a client could otherwise echo its own
        credential into the audit stream; an ID equal to the secret is always
        replaced instead.
        """

        if (
            isinstance(supplied, str)
            and 0 < len(supplied) <= MAX_REQUEST_ID_LENGTH
            and _REQUEST_ID_UNSAFE.search(supplied) is None
            and not (secret is not None and supplied == secret)
        ):
            return supplied

        generated = _REQUEST_ID_UNSAFE.sub("-", str(self._request_ids()))
        generated = generated[:MAX_REQUEST_ID_LENGTH]
        if not generated:
            generated = _new_uuid()
        return generated

    def record(
        self,
        *,
        request_id,
        actor_id,
        action,
        target,
        status,
        started_at,
        code=None
    ):
        """Append one sanitized event line, never raising to the caller.

        Auditing happens after the business response has been written, and a
        broken or slow sink must not fail the request or force a retry.
        """

        try:
            finished_at = self._clock()
            event = {
                "time": _format_time(finished_at),
                "requestId": sanitize(request_id, MAX_REQUEST_ID_LENGTH),
                "actorId": sanitize(actor_id),
                "action": sanitize(action, MAX_ACTION_LENGTH),
                "target": sanitize(target),
                "status": status,
                "code": sanitize(code, MAX_ACTION_LENGTH),
                "durationMs": _duration_ms(started_at, finished_at),
            }
            line = json.dumps(
                event, ensure_ascii=True, separators=(",", ":")
            ) + "\n"
            with self._lock:
                self._output.write(line)
                self._output.flush()
        except Exception:
            # A failed audit must not leak an error or change the response.
            pass


def _utcnow():
    return datetime.now(timezone.utc)


def _new_uuid():
    return uuid.uuid4().hex
