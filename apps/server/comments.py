"""Immutable Comment operations for PicoPG.

A Comment is feedback attached to exactly one Submission.  Comments are
append-only: this module exposes no update, delete, or status behavior.  A
Teacher may create feedback only for a Submission under a Release in a Class
the Teacher currently owns; the actor is stored as the author and the source is
fixed to ``"teacher"`` by the call itself, so a caller can never choose or
spoof it.

AI and system feedback can only be produced through a separate internal
function that requires an unforgeable, in-process trusted capability supplied
by server integration.  That integration is deliberately not enabled: no
actor, request field, or serialized value can supply the capability, so a
bearer user can never select an AI/system source.

Comment bodies can contain student source excerpts, so Admin management access
is not source-review permission.  The Admin role is denied comment body access
for both creation and reads.  Reads are filtered to the owning Student or the
Submission's currently owning Class Teacher.

Creation runs through the persistent idempotency layer, so an exact retry
returns the stored record and a changed request under the same key conflicts.
Bodies are validated only as non-empty, bounded UTF-8 and are stored verbatim;
no Markdown, HTML, or other markup is interpreted.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import accounts
import idempotency
import submissions


__all__ = [
    "Comment",
    "CommentError",
    "InvalidCommentRequestError",
    "COMMENT_CREATE_ENDPOINT",
    "INTERNAL_COMMENT_CREATE_ENDPOINT",
    "MAX_COMMENT_BODY_BYTES",
    "comment_to_mapping",
    "create_comment",
    "create_internal_comment",
    "list_comments",
]


# Teacher feedback is a normal collection under its Submission.
COMMENT_CREATE_ENDPOINT = "/api/v1/submissions/{submission_id}/comments"

# The trusted server integration uses its own scope so an internal result can
# never collide with a bearer-created Teacher comment.
INTERNAL_COMMENT_CREATE_ENDPOINT = "/internal/v1/comments"

# A Comment body is bounded before it reaches the database.  The ceiling is on
# the encoded UTF-8 bytes, not on Python code points.
MAX_COMMENT_BODY_BYTES = 16384

# Only the trusted internal path may name these sources.  ``"teacher"`` is
# deliberately absent: it is set by :func:`create_comment`, never by a caller.
_INTERNAL_SOURCES = ("ai", "system")


class CommentError(Exception):
    """Base class for Comment domain failures."""


class InvalidCommentRequestError(CommentError, ValueError):
    """A Comment request carried a malformed field, body, or time value."""


class _TrustedCommentSourceCapability:
    """Opaque, in-process-only marker for AI/system comment creation.

    The singleton below is the only value accepted by
    :func:`create_internal_comment`.  It has no public constructor, cannot be
    expressed in JSON or a request field, and is intentionally not wired to
    any HTTP route.  A bearer user therefore cannot obtain or fake it.
    """

    __slots__ = ()


_TRUSTED_COMMENT_SOURCE_CAPABILITY = _TrustedCommentSourceCapability()


@dataclass(frozen=True)
class Comment:
    """Immutable metadata for one Submission feedback record."""

    id: str
    submission_id: str
    author_user_id: str | None
    source: str
    body: str
    created_at: str


def comment_to_mapping(comment):
    """Return the canonical camelCase mapping for a Comment."""
    return {
        "id": comment.id,
        "submissionId": comment.submission_id,
        "authorUserId": comment.author_user_id,
        "source": comment.source,
        "body": comment.body,
        "createdAt": comment.created_at,
    }


# --- validation helpers ----------------------------------------------------


def _format_time(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _now():
    return _format_time(datetime.now(timezone.utc))


def _normalize_now(value):
    """Return a canonical UTC RFC3339 string for the stored timestamp."""

    if value is None:
        return _now()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidCommentRequestError("now must include a timezone")
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise InvalidCommentRequestError("now must not be empty")
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as error:
            raise InvalidCommentRequestError(
                "now must be an RFC3339 timestamp"
            ) from error
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise InvalidCommentRequestError("now must include a timezone")
    else:
        raise InvalidCommentRequestError(
            "now must be a timestamp or RFC3339 string"
        )
    return _format_time(moment.astimezone(timezone.utc))


def _require_actor(actor):
    if not isinstance(actor, accounts.User):
        raise accounts.ForbiddenActionError("valid account required")
    return actor


def _require_teacher(actor):
    if not isinstance(actor, accounts.User) or actor.role != "teacher":
        raise accounts.ForbiddenActionError("teacher role required")
    return actor


def _require_identifier(value, field):
    if not isinstance(value, str) or not value:
        raise InvalidCommentRequestError(
            f"{field} must be a non-empty string"
        )
    return value


def _validate_body(body):
    """Return one non-empty, bounded UTF-8 body verbatim.

    The bytes are checked only for UTF-8 validity and size.  Nothing is
    stripped, escaped, or parsed, so Markdown/HTML in the body is never
    interpreted or rewritten.
    """

    if not isinstance(body, str):
        raise InvalidCommentRequestError("body must be a string")
    if body == "":
        raise InvalidCommentRequestError("body must not be empty")
    try:
        encoded = body.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidCommentRequestError("body must be valid UTF-8") from error
    if len(encoded) > MAX_COMMENT_BODY_BYTES:
        raise InvalidCommentRequestError(
            f"body must be at most {MAX_COMMENT_BODY_BYTES} bytes"
        )
    return body


# --- database lookups ------------------------------------------------------


def _submission_row(connection, submission_id):
    return connection.execute(
        "SELECT * FROM submissions WHERE id = ?", (submission_id,)
    ).fetchone()


def _submission_class_teacher(connection, submission_id):
    """Return the current owning Teacher row for a Submission, or ``None``."""

    return connection.execute(
        """
        SELECT classes.teacher_id AS teacher_id
        FROM submissions
        JOIN releases ON releases.id = submissions.release_id
        JOIN classes ON classes.id = releases.class_id
        WHERE submissions.id = ?
        """,
        (submission_id,),
    ).fetchone()


def _comment_from_row(row):
    return Comment(
        id=row["id"],
        submission_id=row["submission_id"],
        author_user_id=row["author_user_id"],
        source=row["source"],
        body=row["body"],
        created_at=row["created_at"],
    )


def _insert_comment(
    connection, submission_id, author_user_id, source, body, created_at
):
    comment = Comment(
        id=uuid.uuid4().hex,
        submission_id=submission_id,
        author_user_id=author_user_id,
        source=source,
        body=body,
        created_at=created_at,
    )
    connection.execute(
        """
        INSERT INTO comments (
            id, submission_id, author_user_id, source, body, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            comment.id,
            comment.submission_id,
            comment.author_user_id,
            comment.source,
            comment.body,
            comment.created_at,
        ),
    )
    return comment


# --- creation --------------------------------------------------------------


def create_comment(
    connection, actor, submission_id, body, request_key, *, now=None
):
    """Create one immutable Teacher Comment, idempotently.

    Only a Teacher may create, and only when the Submission's Release belongs
    to a Class the Teacher owns at call time.  ``source`` is fixed to
    ``"teacher"`` and ``author_user_id`` to the authenticated actor, so neither
    can be supplied, spoofed, or reselected by a caller.  The row and the
    idempotency result commit or roll back together; an exact retry returns the
    stored ``201`` body and a changed body raises
    :class:`idempotency.IdempotencyConflict`.
    """

    actor = _require_teacher(actor)
    submission_id = _require_identifier(submission_id, "submissionId")
    body = _validate_body(body)
    created_at = _normalize_now(now)

    endpoint = COMMENT_CREATE_ENDPOINT.format(submission_id=submission_id)
    request = {
        "submissionId": submission_id,
        "source": "teacher",
        "body": body,
    }

    def callback(business_connection):
        if _submission_row(business_connection, submission_id) is None:
            raise submissions.MissingSubmissionError(submission_id)
        owner = _submission_class_teacher(business_connection, submission_id)
        if owner is None or owner["teacher_id"] != actor.id:
            raise accounts.ForbiddenActionError(
                "teacher does not own the submission class"
            )
        comment = _insert_comment(
            business_connection,
            submission_id,
            actor.id,
            "teacher",
            body,
            created_at,
        )
        return 201, {"comment": comment_to_mapping(comment)}

    return idempotency.execute(
        connection, actor.id, endpoint, request_key, request, callback
    )


def create_internal_comment(
    connection,
    capability,
    service_user_id,
    submission_id,
    source,
    body,
    request_key,
    *,
    now=None,
):
    """Create an AI/system Comment through the trusted internal path.

    ``capability`` must be the module's unforgeable in-process singleton; a
    bearer user, request field, or serialized value can never provide it, so
    this integration stays disabled until server code explicitly holds it.
    ``source`` may be only ``"ai"`` or ``"system"`` and the stored comment has
    no author user.  ``service_user_id`` is the integrating service account
    used only to scope persistent idempotency; it is never stored as the
    comment author.  The Submission must exist.
    """

    if capability is not _TRUSTED_COMMENT_SOURCE_CAPABILITY:
        raise accounts.ForbiddenActionError(
            "trusted server capability required"
        )
    if source not in _INTERNAL_SOURCES:
        raise InvalidCommentRequestError("source must be ai or system")
    service_user_id = _require_identifier(service_user_id, "serviceUserId")
    submission_id = _require_identifier(submission_id, "submissionId")
    body = _validate_body(body)
    created_at = _normalize_now(now)

    if (
        connection.execute(
            "SELECT 1 FROM users WHERE id = ?", (service_user_id,)
        ).fetchone()
        is None
    ):
        raise accounts.MissingUserError(service_user_id)

    endpoint = INTERNAL_COMMENT_CREATE_ENDPOINT
    request = {
        "submissionId": submission_id,
        "source": source,
        "body": body,
    }

    def callback(business_connection):
        if _submission_row(business_connection, submission_id) is None:
            raise submissions.MissingSubmissionError(submission_id)
        comment = _insert_comment(
            business_connection,
            submission_id,
            None,
            source,
            body,
            created_at,
        )
        return 201, {"comment": comment_to_mapping(comment)}

    return idempotency.execute(
        connection, service_user_id, endpoint, request_key, request, callback
    )


# --- reads -----------------------------------------------------------------


def list_comments(connection, actor, submission_id):
    """Return one Submission's Comments ordered by ``(created_at, id)``.

    A Student may read only their own Submission and a Teacher only a
    Submission in a Class they currently own.  Admin is denied because feedback
    can carry student source excerpts.  A missing or non-visible Submission
    raises the same :class:`submissions.MissingSubmissionError`, so the
    existence of another Student's work is not revealed.
    """

    _require_actor(actor)
    submission_id = _require_identifier(submission_id, "submissionId")

    if actor.role == "admin":
        raise accounts.ForbiddenActionError(
            "admin role has no comment body access"
        )
    if actor.role == "student":
        row = _submission_row(connection, submission_id)
        if row is None or row["student_id"] != actor.id:
            raise submissions.MissingSubmissionError(submission_id)
    elif actor.role == "teacher":
        owner = _submission_class_teacher(connection, submission_id)
        if owner is None or owner["teacher_id"] != actor.id:
            raise submissions.MissingSubmissionError(submission_id)
    else:
        raise accounts.ForbiddenActionError("unknown role")

    rows = connection.execute(
        """
        SELECT * FROM comments
        WHERE submission_id = ?
        ORDER BY created_at, id
        """,
        (submission_id,),
    ).fetchall()
    return [_comment_from_row(row) for row in rows]
