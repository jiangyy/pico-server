"""The per-kind handlers the sync engine delegates to.

Only ``submit_code`` is handled here. ``publish_playground`` and
``archive_playground`` are operator actions that arrive through the
publisher's own HTTP endpoints, never through a client's sync channel, so
their handlers refuse anything a client submits under those kinds.

Every handler runs inside the engine's open transaction and writes only
through the connection it is given; none of them commits. A handler
returns the JSON body that is stored as the event's ``response_json`` and
replayed verbatim on a retry, or raises :class:`sync.EventRejected` with a
stable reason code.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import events_types
import playgrounds as playgrounds_module
import repository as repository_module
import submissions as submissions_module
import sync

__all__ = [
    "submit_handler",
    "publish_handler",
    "archive_handler",
]


def submit_handler(connection, envelope, *, repository):
    """Persist a ``submit_code`` event as one immutable Submission.

    The order is deliberate: validate the payload's shape, derive the
    student from the hand-in's own header, then re-check the facts only the
    database and the repository know — the Playground exists, it is not
    archived, and the named version really is in that Playground's published
    history. Nothing is written unless every check passes.
    """
    payload = envelope.payload
    playground_id = payload.get("playground_id")
    version_id = payload.get("version_id")
    files = payload.get("files")

    if type(playground_id) is not str or not playground_id:
        raise sync.EventRejected(events_types.REASON_INVALID_SUBMISSION)
    if type(version_id) is not str or not version_id:
        raise sync.EventRejected(events_types.REASON_INVALID_SUBMISSION)
    if type(files) is not dict:
        raise sync.EventRejected(events_types.REASON_INVALID_SUBMISSION)

    student = submissions_module.student_from_files(files)
    if student is None:
        raise sync.EventRejected(events_types.REASON_INVALID_SUBMISSION)

    # The client sends snake_case (protocol.md); the submission domain's
    # own validator speaks the camelCase the legacy endpoint used. Building
    # the mapping here reuses that one validator instead of writing a second
    # set of path and size rules that could drift from it.
    try:
        request = submissions_module.decode_submission_request(
            {
                "playgroundId": playground_id,
                "commitId": version_id,
                "student": student,
                "files": files,
            }
        )
    except submissions_module.InvalidSubmissionRequestError:
        raise sync.EventRejected(events_types.REASON_INVALID_SUBMISSION)

    playground = playgrounds_module.get_playground(connection, request.playground_id)
    if playground is None:
        raise sync.EventRejected(events_types.REASON_INVALID_VERSION)
    if playground.state == "stopped":
        raise sync.EventRejected(events_types.REASON_PLAYGROUND_STOPPED)
    if not repository_module.commit_in_playground_history(
        repository, request.playground_id, request.playground_commit_id
    ):
        raise sync.EventRejected(events_types.REASON_INVALID_VERSION)

    submission_id = uuid4().hex
    created_at = _now()
    files_json = json.dumps(dict(request.files), separators=(",", ":"), sort_keys=True)

    connection.execute(
        """
        INSERT INTO submissions (
            id, playground_id, playground_commit_id, student,
            files_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            submission_id,
            request.playground_id,
            request.playground_commit_id,
            request.student,
            files_json,
            created_at,
        ),
    )

    return {
        "submission": {
            "id": submission_id,
            "playground_id": request.playground_id,
            "version_id": request.playground_commit_id,
            "student": request.student,
            "created_at": created_at,
        }
    }


def publish_handler(connection, envelope):
    """A client cannot publish; only the publisher's own endpoint does."""
    raise sync.EventRejected(events_types.REASON_INVALID_EVENT)


def archive_handler(connection, envelope):
    """A client cannot archive; only the operator's own endpoint does."""
    raise sync.EventRejected(events_types.REASON_INVALID_EVENT)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
