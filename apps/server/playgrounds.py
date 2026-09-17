"""Playground metadata domain logic."""

from dataclasses import dataclass
from datetime import datetime, timezone

import repository as _repository
from accounts import ForbiddenActionError, User


RELEASE_ONLY_POLICY = "release_only"
PUBLIC_POLICY = "public"
PLAYGROUND_POLICIES = (RELEASE_ONLY_POLICY, PUBLIC_POLICY)


class InvalidRequestError(ValueError):
    """A mutation carried a malformed identifier or body."""


class InvalidPolicyError(ValueError):
    """A requested playground policy is not supported."""


class MissingPlaygroundError(Exception):
    """No playground exists for the requested identifier."""


class PlaygroundConflictError(Exception):
    """A playground identifier is already in use."""


class StoredPlaygroundError(Exception):
    """A durable Playground row cannot be represented by the admin API."""


@dataclass(frozen=True)
class Playground:
    id: str
    creator_id: str
    policy: str
    created_at: str
    archived_at: str | None


@dataclass(frozen=True)
class AdminPlayground:
    """One bounded playground record for admin discovery."""

    id: str
    archived: bool
    created_at: str


@dataclass(frozen=True)
class PlaygroundPage:
    """One keyset page of playgrounds plus the exclusive next key."""

    items: tuple
    next_key: str | None


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_opaque_id(value):
    if not _repository.valid_playground_id(value):
        raise InvalidRequestError("playground id is not valid")
    return value


def _require_actor(actor):
    if not isinstance(actor, User):
        raise ForbiddenActionError("valid account required")
    return actor


def _require_admin(actor):
    _require_actor(actor)
    if actor.role != "admin":
        raise ForbiddenActionError("admin role required")


def _require_teacher_or_admin(actor):
    _require_actor(actor)
    if actor.role not in ("teacher", "admin"):
        raise ForbiddenActionError("teacher or admin role required")


def _playground_row(connection, playground_id):
    playground_id = _require_opaque_id(playground_id)
    return connection.execute(
        "SELECT id, creator_id, policy, created_at, archived_at"
        " FROM playgrounds WHERE id = ?",
        (playground_id,),
    ).fetchone()


def _playground_from_row(row):
    return Playground(
        id=row["id"],
        creator_id=row["creator_id"],
        policy=row["policy"],
        created_at=row["created_at"],
        archived_at=row["archived_at"],
    )


def list_playgrounds(connection, actor, *, limit, after=None):
    """Return one Admin-only keyset page of playgrounds ordered by id.

    The query uses ``LIMIT limit + 1`` at the authoritative boundary.  Only
    the opaque id and archived flag are returned; a durable row whose id does
    not satisfy the Playground identifier rules fails closed instead of being
    silently skipped.
    """

    _require_admin(actor)
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")

    parameters = []
    where = ""
    if after is not None:
        where = "WHERE id > ?"
        parameters.append(after)
    parameters.append(limit + 1)
    rows = connection.execute(
        "SELECT id, archived_at, created_at FROM playgrounds "
        f"{where} ORDER BY id LIMIT ?",
        parameters,
    ).fetchall()

    page_rows = rows[:limit]
    next_key = page_rows[-1]["id"] if len(rows) > limit else None
    items = []
    for row in page_rows:
        if not _repository.valid_playground_id(row["id"]):
            raise StoredPlaygroundError(
                "stored playground identifier is invalid"
            )
        items.append(
            AdminPlayground(
                id=row["id"],
                archived=row["archived_at"] is not None,
                created_at=row["created_at"],
            )
        )
    return PlaygroundPage(items=tuple(items), next_key=next_key)


def list_releasable_playground_ids(connection, actor, *, limit, after=None):
    """Return one stable page of unarchived Playground ids for release."""

    _require_teacher_or_admin(actor)
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")

    parameters = []
    where = "WHERE archived_at IS NULL"
    if after is not None:
        where += " AND id > ?"
        parameters.append(after)
    parameters.append(limit + 1)
    rows = connection.execute(
        "SELECT id FROM playgrounds "
        f"{where} ORDER BY id LIMIT ?",
        parameters,
    ).fetchall()

    page_rows = rows[:limit]
    for row in page_rows:
        if not _repository.valid_playground_id(row["id"]):
            raise StoredPlaygroundError(
                "stored playground identifier is invalid"
            )
    return PlaygroundPage(
        items=tuple(row["id"] for row in page_rows),
        next_key=(
            page_rows[-1]["id"] if len(rows) > limit else None
        ),
    )


def create_playground(connection, actor, playground_id):
    """Create release-only playground metadata for an Admin actor."""
    _require_admin(actor)
    playground_id = _require_opaque_id(playground_id)

    with connection:
        row = connection.execute(
            "SELECT 1 FROM playgrounds WHERE id = ?",
            (playground_id,),
        ).fetchone()
        if row is not None:
            raise PlaygroundConflictError("playground already exists")

        created_at = _now()
        connection.execute(
            "INSERT INTO playgrounds"
            " (id, creator_id, policy, created_at, archived_at)"
            " VALUES (?, ?, ?, ?, NULL)",
            (playground_id, actor.id, RELEASE_ONLY_POLICY, created_at),
        )

    return Playground(
        id=playground_id,
        creator_id=actor.id,
        policy=RELEASE_ONLY_POLICY,
        created_at=created_at,
        archived_at=None,
    )


def update_playground(connection, actor, playground_id, mutation):
    """Apply exactly one policy or archive mutation to existing metadata."""
    _require_actor(actor)
    playground_id = _require_opaque_id(playground_id)
    if not isinstance(mutation, dict):
        raise InvalidRequestError("mutation must be a mapping")

    keys = set(mutation)
    if keys == {"policy"}:
        policy = mutation["policy"]
        if policy not in PLAYGROUND_POLICIES:
            raise InvalidPolicyError("unsupported playground policy")
        archived = None
    elif keys == {"archived"}:
        policy = None
        archived = mutation["archived"]
        if not isinstance(archived, bool):
            raise InvalidRequestError("archived must be a boolean")
    else:
        raise InvalidRequestError("expected exactly one mutation field")

    with connection:
        row = _playground_row(connection, playground_id)
        if row is None:
            raise MissingPlaygroundError("playground does not exist")

        _require_admin(actor)

        if policy is not None:
            connection.execute(
                "UPDATE playgrounds SET policy = ? WHERE id = ?",
                (policy, playground_id),
            )
        elif archived:
            if row["archived_at"] is None:
                connection.execute(
                    "UPDATE playgrounds SET archived_at = ? WHERE id = ?",
                    (_now(), playground_id),
                )
        else:
            if row["archived_at"] is not None:
                connection.execute(
                    "UPDATE playgrounds SET archived_at = NULL WHERE id = ?",
                    (playground_id,),
                )

        updated = _playground_row(connection, playground_id)

    return _playground_from_row(updated)


def publish_playground(
    connection, actor, playground_id, repository, bundle, metadata
):
    """Publish a validated bundle to a Playground's Git history.

    Admin authorization and unarchived metadata are resolved before any Git
    subprocess is started.  The Git work itself lives in
    :func:`repository.publish_playground`.
    """

    _require_admin(actor)
    playground_id = _require_opaque_id(playground_id)

    row = _playground_row(connection, playground_id)
    if row is None:
        raise MissingPlaygroundError("playground does not exist")
    if row["archived_at"] is not None:
        raise ForbiddenActionError("playground is archived")

    return _repository.publish_playground(
        repository, playground_id, bundle, metadata
    )
