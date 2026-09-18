"""Published Playground metadata.

A row here only records that an operator published a bundle under an opaque
identifier; every version of the content itself lives in the Playground's Git
ref.  There is no policy, owner, or archive flag left: a published Playground
is readable by any client, so the catalog is exactly the set of rows.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import repository as _repository


class InvalidRequestError(ValueError):
    """A mutation carried a malformed identifier or body."""


class MissingPlaygroundError(Exception):
    """No playground exists for the requested identifier."""


class PlaygroundConflictError(Exception):
    """A playground identifier is already in use."""


class StoredPlaygroundError(Exception):
    """A durable Playground row cannot be represented by the API."""


@dataclass(frozen=True)
class Playground:
    id: str
    created_at: str


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_opaque_id(value):
    if not _repository.valid_playground_id(value):
        raise InvalidRequestError("playground id is not valid")
    return value


def _playground_row(connection, playground_id):
    playground_id = _require_opaque_id(playground_id)
    return connection.execute(
        "SELECT id, created_at FROM playgrounds WHERE id = ?",
        (playground_id,),
    ).fetchone()


def _playground_from_row(row):
    return Playground(id=row["id"], created_at=row["created_at"])


def get_playground(connection, playground_id):
    """Return one stored Playground, or ``None`` when it does not exist."""

    row = _playground_row(connection, playground_id)
    return None if row is None else _playground_from_row(row)


def create_playground(connection, playground_id):
    """Create the metadata row for a new Playground.

    The identifier is validated before it reaches SQL or a Git ref, and a
    duplicate is reported instead of overwriting an existing history.
    """

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
            "INSERT INTO playgrounds (id, created_at) VALUES (?, ?)",
            (playground_id, created_at),
        )

    return Playground(id=playground_id, created_at=created_at)


def publish_playground(connection, playground_id, repository, bundle, metadata):
    """Publish a validated bundle to a Playground's Git history.

    The metadata row is resolved before any Git subprocess is started, so an
    unknown Playground never reaches the repository.  The Git work itself lives
    in :func:`repository.publish_playground`.
    """

    playground_id = _require_opaque_id(playground_id)

    if _playground_row(connection, playground_id) is None:
        raise MissingPlaygroundError("playground does not exist")

    return _repository.publish_playground(
        repository, playground_id, bundle, metadata
    )
