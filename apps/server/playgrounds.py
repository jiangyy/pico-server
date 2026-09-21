"""Published Playground metadata.

A row here only records that an operator published a bundle under an opaque
identifier; every version of the content itself lives in the Playground's Git
ref.  There is no policy, owner, or archive flag left: a published Playground
is readable by any client, so the catalog is exactly the set of rows.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

import database
import events
import events_types
import repository as _repository


class InvalidRequestError(ValueError):
    """A mutation carried a malformed identifier or body."""


class MissingPlaygroundError(Exception):
    """No playground exists for the requested identifier."""


class PlaygroundConflictError(Exception):
    """A playground identifier is already in use."""


class PlaygroundStoppedError(Exception):
    """The Playground is archived, so it accepts no new Version."""


class PlaygroundBaseChangedError(Exception):
    """The publisher's ``base_version_id`` is not the current Version.

    protocol.md's ``conflict``: the publisher built its bundle on a Version
    that has since been replaced, so publishing it would silently drop the
    other change. It is a refusal the publisher resolves by reading the current
    Version and deciding again, not by retrying the same request.
    """


@dataclass(frozen=True)
class Playground:
    id: str
    created_at: str
    state: str = "active"
    current_version_id: str = ""


#: A Version's identity is a Git object id in this implementation; a base that
#: is not one is a malformed request, not a stale one.
_VERSION_ID_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z|\A[0-9a-f]{64}\Z")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_opaque_id(value):
    if not _repository.valid_playground_id(value):
        raise InvalidRequestError("playground id is not valid")
    return value


def _playground_row(connection, playground_id):
    playground_id = _require_opaque_id(playground_id)
    return connection.execute(
        "SELECT id, created_at, state, current_version_id "
        "FROM playgrounds WHERE id = ?",
        (playground_id,),
    ).fetchone()


def _playground_from_row(row):
    return Playground(
        id=row["id"],
        created_at=row["created_at"],
        state=row["state"],
        current_version_id=row["current_version_id"],
    )


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

    with database.immediate_transaction(connection):
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


def publish_playground(
    connection, playground_id, repository, bundle, metadata, base_version_id=None
):
    """Publish a validated bundle as the Playground's next Version.

    protocol.md §2: the publisher says which Version it built on, and the
    publish only lands if that is still the current one. Without it two
    publishers racing would each append a Version and the later one would
    silently discard the earlier one's content — with it, the loser is told
    ``conflict`` and can look at what arrived before deciding again.

    The metadata row is resolved before any Git subprocess is started, so an
    unknown Playground never reaches the repository. On a fresh publish
    (``no_op=False``) the new Version is recorded as a ``publish_playground``
    event in the content log and ``current_version_id`` is updated; the event id
    is generated here because an operator's request carries none.

    Raises :class:`PlaygroundStoppedError` for an archived Playground,
    :class:`PlaygroundBaseChangedError` for a stale base, and
    :class:`InvalidRequestError` for a base that is missing when one is needed
    or malformed. The caller owns the transaction; this never commits.
    """

    playground_id = _require_opaque_id(playground_id)

    row = _playground_row(connection, playground_id)
    if row is None:
        raise MissingPlaygroundError("playground does not exist")
    if row["state"] == "stopped":
        raise PlaygroundStoppedError("playground is archived")

    _require_base(base_version_id, row["current_version_id"])

    result = _repository.publish_playground(
        repository, playground_id, bundle, metadata
    )

    if not result.no_op:
        record_publish_event(connection, playground_id, result.commit_id)

    return result


def _require_base(base_version_id, observed_version_id):
    """Refuse a publish whose base is missing where one is needed, or stale.

    A Playground with no Version yet has nothing to be stale against, so its
    first publish must *not* name a base — naming one would claim a history that
    does not exist. Every later publish must name the current Version: that is
    the whole point of the field, so leaving it out is a malformed request
    rather than something to guess at.
    """

    if base_version_id and not _VERSION_ID_PATTERN.match(base_version_id):
        raise InvalidRequestError("base_version_id is not a version id")

    if not observed_version_id:
        if base_version_id:
            raise PlaygroundBaseChangedError(
                "playground has no version to base on"
            )
        return

    if not base_version_id:
        raise InvalidRequestError("base_version_id is required")
    if base_version_id != observed_version_id:
        raise PlaygroundBaseChangedError("base_version_id is not current")


def restore_playground(connection, playground_id):
    """Bring an archived Playground back into the catalog.

    The counterpart to Archive. ``state`` is a distribution flag, and something
    has to be able to clear it: publishing is refused while a Playground is
    stopped (protocol.md §2), so without this the flag only moves one way and a
    retired course can never come back — which is what `retire.mjs` used to
    promise and could not deliver.

    Returns whether it changed anything: ``False`` for a Playground that is
    already active or was cancelled by a concurrent archive.

    Emits a ``publish_playground`` event for the Version it returns to. That is
    what tells a client the course is available again — the content log has no
    separate "restored" kind, and a client already knows what to do with a
    publish: install that Version and put it back in the active set.
    """

    playground_id = _require_opaque_id(playground_id)

    with database.immediate_transaction(connection):
        row = _playground_row(connection, playground_id)
        if row is None:
            raise MissingPlaygroundError("playground does not exist")
        if row["state"] == "active":
            return False

        connection.execute(
            "UPDATE playgrounds SET state = 'active' WHERE id = ?",
            (playground_id,),
        )
        # A Playground that was never published has no Version to announce; it
        # becomes active and stays out of the catalog until something is.
        if row["current_version_id"]:
            record_publish_event(connection, playground_id, row["current_version_id"])
        return True


def backfill_current_versions(connection, resolve_version):
    """Remember the current version of every row that never stored one.

    ``current_version_id`` is written by every publish and read by the catalog,
    so the catalog never has to ask Git which commit a Playground is on. Rows
    from before that column existed have no value; resolving those costs one Git
    subprocess each, so it is done once here at start-up rather than once per row
    per catalog request.

    ``resolve_version(playground_id)`` returns the version id or ``None`` when
    the Playground has no readable ref. A row that cannot be resolved is left
    alone: the catalog skips it, exactly as it did before.

    Returns how many rows were filled in.
    """
    rows = connection.execute(
        "SELECT id FROM playgrounds "
        "WHERE current_version_id = '' AND state = 'active'"
    ).fetchall()

    filled = 0
    with connection:
        for row in rows:
            version_id = resolve_version(row["id"])
            if version_id is None:
                continue
            connection.execute(
                "UPDATE playgrounds SET current_version_id = ? WHERE id = ?",
                (version_id, row["id"]),
            )
            filled += 1
    return filled


def record_publish_event(connection, playground_id, version_id):
    """Record a server-generated ``publish_playground`` event.

    Writes the event row, appends to the content log and updates the
    playground's ``current_version_id``. Runs in the caller's transaction
    and never commits, so the event lands only if the publish that produced
    it does too. Returns the generated event id.
    """
    envelope = events_types.EventEnvelope(
        event_id=uuid4().hex,
        kind=events_types.EVENT_KIND_PUBLISH_PLAYGROUND,
        payload={"playground_id": playground_id, "version_id": version_id},
    )
    events.accept_event(
        connection,
        envelope,
        response_status="accepted",
        response_payload={"version_id": version_id},
    )
    events.append_content_log(connection, envelope)
    connection.execute(
        "UPDATE playgrounds SET current_version_id = ? WHERE id = ?",
        (version_id, playground_id),
    )
    return envelope.event_id


def record_archive_event(connection, playground_id):
    """Record a server-generated ``archive_playground`` event.

    Writes the event row, appends to the content log and flips the playground
    to ``state = stopped``. The Git ref is left intact: archive means "stop
    using it", not "delete the bytes". Runs in the caller's transaction and
    never commits.
    """
    envelope = events_types.EventEnvelope(
        event_id=uuid4().hex,
        kind=events_types.EVENT_KIND_ARCHIVE_PLAYGROUND,
        payload={"playground_id": playground_id},
    )
    events.accept_event(
        connection,
        envelope,
        response_status="accepted",
        response_payload={"playground_id": playground_id},
    )
    events.append_content_log(connection, envelope)
    connection.execute(
        "UPDATE playgrounds SET state = 'stopped' WHERE id = ?",
        (playground_id,),
    )
    return envelope.event_id
