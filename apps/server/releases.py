"""Immutable Release operations for PicoPG.

A Release pins one Class to the exact Playground commit that was chosen when it
was created.  Creation is authorized, transactional, and idempotent through the
task-10 ``idempotency`` module; the stored commit is never replaced by the
Playground's current HEAD.  Releases are append-only: this module deliberately
exposes no update or delete behavior.

The Git side is reached through the repository adapter (:class:`repository.
Repository`).  The adapter validates the Playground ref and the exact commit;
the domain never guesses a schema, route, or branch.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import accounts
import classes
import idempotency
import playgrounds
import repository as repository_module
from pagination import Page


RELEASE_CREATE_ENDPOINT = "/api/v1/classes/{class_id}/releases"

# Git object IDs are lowercase hexadecimal.  Only the exact object-id text that
# Git itself prints is accepted, so no value can be mistaken for a ref or an
# option when it reaches the repository adapter.
_COMMIT_ID_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z|\A[0-9a-f]{64}\Z")


class ReleaseError(Exception):
    """Base class for Release domain failures."""


class InvalidReleaseRequestError(ReleaseError, ValueError):
    """A Release request carried a malformed field or timestamp."""


class UnknownPlaygroundCommitError(ReleaseError):
    """The supplied commit does not exist on the target Playground history."""


class MissingReleaseError(ReleaseError):
    """No readable Release exists for the requested identifier."""


@dataclass(frozen=True)
class Release:
    """Immutable metadata for one Class/Playground commit pairing."""

    id: str
    class_id: str
    playground_id: str
    playground_commit_id: str
    created_by: str
    open_at: str
    due_at: str | None
    requires_submission: bool
    created_at: str


def release_to_mapping(release):
    """Return the canonical camelCase mapping for a Release."""
    return {
        "id": release.id,
        "classId": release.class_id,
        "playgroundId": release.playground_id,
        "playgroundCommitId": release.playground_commit_id,
        "createdBy": release.created_by,
        "openAt": release.open_at,
        "dueAt": release.due_at,
        "requiresSubmission": release.requires_submission,
        "createdAt": release.created_at,
    }


def _format_time(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _now():
    return _format_time(datetime.now(timezone.utc))


def normalize_timestamp(value, field):
    """Return a UTC RFC3339 string, rejecting non-timezone-aware values."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidReleaseRequestError(
                f"{field} must include a timezone"
            )
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise InvalidReleaseRequestError(f"{field} must not be empty")
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as error:
            raise InvalidReleaseRequestError(
                f"{field} must be an RFC3339 timestamp"
            ) from error
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise InvalidReleaseRequestError(
                f"{field} must include a timezone"
            )
    else:
        raise InvalidReleaseRequestError(
            f"{field} must be a timestamp or RFC3339 string"
        )
    return _format_time(moment.astimezone(timezone.utc))


def _require_actor(actor):
    if not isinstance(actor, accounts.User):
        raise accounts.ForbiddenActionError("valid account required")
    return actor


def _require_identifier(value, field):
    if not isinstance(value, str) or not value:
        raise InvalidReleaseRequestError(f"{field} must be a non-empty string")
    return value


def _require_commit_id(value):
    value = _require_identifier(value, "playgroundCommitId")
    if _COMMIT_ID_PATTERN.match(value) is None:
        raise InvalidReleaseRequestError(
            "playgroundCommitId must be a Git object id"
        )
    return value


def _require_repository(repository):
    if not isinstance(repository, repository_module.Repository):
        raise InvalidReleaseRequestError("repository adapter required")
    return repository


def _require_class(connection, class_id):
    row = connection.execute(
        "SELECT * FROM classes WHERE id = ?", (class_id,)
    ).fetchone()
    if row is None:
        raise classes.MissingClassError(class_id)
    return row


def _authorize_owner(connection, actor, class_id):
    if actor.role != "teacher":
        raise accounts.ForbiddenActionError("teacher role required")
    row = _require_class(connection, class_id)
    if row["teacher_id"] != actor.id:
        raise accounts.ForbiddenActionError("teacher does not own class")
    return row


def _require_playground(connection, playground_id):
    row = connection.execute(
        "SELECT id, creator_id, archived_at FROM playgrounds WHERE id = ?",
        (playground_id,),
    ).fetchone()
    if row is None:
        raise playgrounds.MissingPlaygroundError("playground does not exist")
    return row


def _authorize_playground(connection, actor, playground_id):
    """Reject a missing or archived Playground.

    The caller has already proven that the actor is a Teacher who owns the
    target Class; any such Teacher may release any existing, unarchived
    Playground.  The ``actor`` parameter is kept for call-site symmetry.
    """
    row = _require_playground(connection, playground_id)
    if row["archived_at"] is not None:
        raise accounts.ForbiddenActionError("playground is archived")


def _require_playground_commit(repository, playground_id, commit_id):
    """Verify the exact commit is a real, reachable Playground commit.

    Membership means the commit object exists and is an ancestor of (or equal
    to) the Playground ref.  The function never resolves or stores the current
    HEAD and never substitutes it for the caller's exact commit.
    """
    try:
        ref = repository.playground_ref(playground_id)
    except repository_module.RepositoryError as error:
        raise InvalidReleaseRequestError(
            "invalid playground identifier"
        ) from error

    completed = repository_module._run_git(
        ("merge-base", "--is-ancestor", commit_id, ref),
        cwd=repository.path,
    )
    if completed.returncode != 0:
        raise UnknownPlaygroundCommitError(
            "commit is not part of the playground history"
        )


def _insert_release(connection, class_id, playground_id, commit_id, actor_id,
                    open_at, due_at, requires_submission):
    release = Release(
        id=uuid.uuid4().hex,
        class_id=class_id,
        playground_id=playground_id,
        playground_commit_id=commit_id,
        created_by=actor_id,
        open_at=open_at,
        due_at=due_at,
        requires_submission=requires_submission,
        created_at=_now(),
    )
    connection.execute(
        """
        INSERT INTO releases (
            id, class_id, playground_id, playground_commit_id, created_by,
            open_at, due_at, requires_submission, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            release.id,
            release.class_id,
            release.playground_id,
            release.playground_commit_id,
            release.created_by,
            release.open_at,
            release.due_at,
            1 if release.requires_submission else 0,
            release.created_at,
        ),
    )
    return release


def _release_from_row(row):
    return Release(
        id=row["id"],
        class_id=row["class_id"],
        playground_id=row["playground_id"],
        playground_commit_id=row["playground_commit_id"],
        created_by=row["created_by"],
        open_at=row["open_at"],
        due_at=row["due_at"],
        requires_submission=bool(row["requires_submission"]),
        created_at=row["created_at"],
    )


def create_release(
    connection,
    actor,
    repository,
    class_id,
    playground_id,
    playground_commit_id,
    open_at,
    requires_submission,
    request_key,
    due_at=None,
):
    """Create one immutable Release, idempotently, in a single transaction.

    Returns ``(status, response)`` exactly as recorded by the idempotency
    layer, so an exact retry replays the original ``201`` body.  Only a Teacher
    who currently owns the target Class and is authorized for the Playground
    may create; reusing a key with a changed body raises
    :class:`idempotency.IdempotencyConflict`.
    """
    _require_actor(actor)
    class_id = _require_identifier(class_id, "classId")
    playground_id = _require_identifier(playground_id, "playgroundId")
    commit_id = _require_commit_id(playground_commit_id)
    if not isinstance(requires_submission, bool):
        raise InvalidReleaseRequestError(
            "requiresSubmission must be a boolean"
        )
    repository = _require_repository(repository)
    open_iso = normalize_timestamp(open_at, "openAt")
    due_iso = None if due_at is None else normalize_timestamp(due_at, "dueAt")
    if due_iso is not None and due_iso < open_iso:
        raise InvalidReleaseRequestError("dueAt must not precede openAt")

    endpoint = RELEASE_CREATE_ENDPOINT.format(class_id=class_id)
    request = {
        "classId": class_id,
        "playgroundId": playground_id,
        "playgroundCommitId": commit_id,
        "openAt": open_iso,
        "dueAt": due_iso,
        "requiresSubmission": requires_submission,
    }

    def callback(business_connection):
        _authorize_owner(business_connection, actor, class_id)
        _authorize_playground(business_connection, actor, playground_id)
        _require_playground_commit(repository, playground_id, commit_id)
        release = _insert_release(
            business_connection,
            class_id,
            playground_id,
            commit_id,
            actor.id,
            open_iso,
            due_iso,
            requires_submission,
        )
        return 201, {"release": release_to_mapping(release)}

    return idempotency.execute(
        connection, actor.id, endpoint, request_key, request, callback
    )


def _normalize_now(now):
    if now is None:
        return _now()
    return normalize_timestamp(now, "now")


def list_releases(
    connection, actor, *, class_id=None, limit=50, after=None, now=None
):
    """Return one bounded page of visible Releases ordered by ``(open_at, id)``.

    ``class_id`` filters to one Class and is also the route binding for the
    class-scoped endpoint.  The authorization predicate is part of the SQL so
    a page is filled with visible rows.
    """

    _require_actor(actor)
    visible_through = _normalize_now(now)
    after_open = None
    after_id = None
    if after is not None:
        if type(after) is not list or len(after) != 2:
            raise ValueError("invalid cursor")
        after_open, after_id = after

    conditions = []
    parameters = []
    if actor.role == "teacher":
        joins = " JOIN classes ON classes.id = releases.class_id"
        conditions.append("classes.teacher_id = ?")
        parameters.append(actor.id)
    elif actor.role == "student":
        joins = (
            " JOIN class_memberships"
            " ON class_memberships.class_id = releases.class_id"
        )
        conditions.append("class_memberships.student_id = ?")
        parameters.append(actor.id)
        conditions.append("releases.open_at <= ?")
        parameters.append(visible_through)
    elif actor.role == "admin":
        joins = ""
    else:
        raise accounts.ForbiddenActionError("unknown role")

    if class_id is not None:
        conditions.append("releases.class_id = ?")
        parameters.append(class_id)
    if after_open is not None:
        conditions.append(
            "(releases.open_at > ? OR"
            " (releases.open_at = ? AND releases.id > ?))"
        )
        parameters.extend([after_open, after_open, after_id])

    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    parameters.append(limit + 1)
    rows = connection.execute(
        "SELECT releases.* FROM releases"
        f"{joins}{where}"
        " ORDER BY releases.open_at, releases.id LIMIT ?",
        parameters,
    ).fetchall()

    page_rows = rows[:limit]
    next_key = (
        [page_rows[-1]["open_at"], page_rows[-1]["id"]]
        if len(rows) > limit
        else None
    )
    return Page(
        items=tuple(_release_from_row(row) for row in page_rows),
        next_key=next_key,
    )


def _row_is_visible(connection, actor, row, visible_through):
    if actor.role == "admin":
        return True
    if actor.role == "teacher":
        owner = connection.execute(
            "SELECT teacher_id FROM classes WHERE id = ?", (row["class_id"],)
        ).fetchone()
        return owner is not None and owner["teacher_id"] == actor.id
    if actor.role == "student":
        membership = connection.execute(
            """
            SELECT 1 FROM class_memberships
            WHERE student_id = ? AND class_id = ?
            """,
            (actor.id, row["class_id"]),
        ).fetchone()
        return membership is not None and row["open_at"] <= visible_through
    raise accounts.ForbiddenActionError("unknown role")


def get_release(connection, actor, release_id, now=None):
    """Return one visible Release, hiding missing and unauthorized records."""
    _require_actor(actor)
    release_id = _require_identifier(release_id, "releaseId")
    visible_through = _normalize_now(now)

    row = connection.execute(
        "SELECT * FROM releases WHERE id = ?", (release_id,)
    ).fetchone()
    if row is None or not _row_is_visible(
        connection, actor, row, visible_through
    ):
        raise MissingReleaseError(release_id)
    return _release_from_row(row)
