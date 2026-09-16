"""Playground metadata and publisher authorization domain logic."""

from dataclasses import dataclass
from datetime import datetime, timezone

import repository as _repository
from accounts import ForbiddenActionError, MissingUserError, User


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


@dataclass(frozen=True)
class Playground:
    id: str
    creator_id: str
    policy: str
    created_at: str
    archived_at: str | None


@dataclass(frozen=True)
class PublisherGrant:
    user_id: str
    granted_by: str
    created_at: str


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_opaque_id(value):
    if not isinstance(value, str) or not value:
        raise InvalidRequestError("identifier must be a non-empty string")
    return value


def _require_actor(actor):
    if not isinstance(actor, User):
        raise ForbiddenActionError("valid account required")
    return actor


def _require_admin(actor):
    _require_actor(actor)
    if actor.role != "admin":
        raise ForbiddenActionError("admin role required")


def _require_user(connection, user_id):
    user_id = _require_opaque_id(user_id)
    row = connection.execute(
        "SELECT id FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        raise MissingUserError("user does not exist")
    return row["id"]


def _require_granted(connection, user_id):
    row = connection.execute(
        "SELECT 1 FROM publisher_grants WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return row is not None


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


def grant_publisher(connection, actor, user_id):
    """Grant publishing permission to an existing user, idempotently."""
    _require_actor(actor)
    with connection:
        _require_admin(actor)
        user_id = _require_user(connection, user_id)
        connection.execute(
            "INSERT OR IGNORE INTO publisher_grants"
            " (user_id, granted_by, created_at) VALUES (?, ?, ?)",
            (user_id, actor.id, _now()),
        )
        row = connection.execute(
            "SELECT user_id, granted_by, created_at"
            " FROM publisher_grants WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return PublisherGrant(
        user_id=row["user_id"],
        granted_by=row["granted_by"],
        created_at=row["created_at"],
    )


def revoke_publisher(connection, actor, user_id):
    """Revoke publishing permission from an existing user, idempotently."""
    _require_actor(actor)
    with connection:
        _require_admin(actor)
        user_id = _require_user(connection, user_id)
        connection.execute(
            "DELETE FROM publisher_grants WHERE user_id = ?",
            (user_id,),
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
