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

#: The storage layer's publish result; re-exported shape, unchanged by the
#: three-phase split.
from repository import PublishResult


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


def require_publishable(connection, playground_id, base_version_id):
    """Check everything the database alone can refuse, and return the row.

    The first of the three publish phases that touches the database, and the
    only one that runs before any Git work: an archived Playground or a stale
    base is refused without creating a commit. That is worth doing up front — a
    refusal must not leave a prepared Version to clean up.

    A Playground that does not exist yet is *not* a refusal: protocol.md §2 step 0
    makes the first publish of an id create it, so this returns ``None`` and the
    commit phase creates the row. What such a request must not carry is a base —
    `_require_base` reads a base with no observed version as a claim about a
    history that does not exist.

    Returns the row, or ``None`` for a Playground that is about to be created, so
    the caller can pass ``current_version_id`` on to the compare-and-swap rather
    than reading it twice.
    """

    playground_id = _require_opaque_id(playground_id)

    row = _playground_row(connection, playground_id)
    if row is None:
        _require_base(base_version_id, "")
        return None
    if row["state"] == "stopped":
        raise PlaygroundStoppedError("playground is archived")

    _require_base(base_version_id, row["current_version_id"])
    return row


def commit_prepared_version(
    connection, playground_id, prepared, base_version_id
):
    """Accept a prepared Version, or refuse it because the base moved.

    The second phase, and the only one that writes: the compare-and-swap on
    ``current_version_id``, the Event and the Content Log entry all land in the
    caller's transaction or none of them do. Until this commits, the Version is
    only a pending ref and the database has never heard of it.

    Refusing here is not an error the caller can retry into success: the
    database says the base it was given is not current, and the publisher has to
    look at what arrived and decide again.
    """

    row = _playground_row(connection, playground_id)
    if row is None:
        return _create_from_prepared(connection, playground_id, prepared, base_version_id)
    if row["state"] == "stopped":
        raise PlaygroundStoppedError("playground is archived")
    if row["current_version_id"] != (base_version_id or ""):
        raise PlaygroundBaseChangedError("base_version_id is not current")

    if prepared.no_op:
        return PublishResult(
            playground_id=playground_id, commit_id=prepared.commit_id, no_op=True
        )

    record_publish_event(
        connection,
        playground_id,
        prepared.commit_id,
        event_id=prepared.event_id,
    )
    return PublishResult(
        playground_id=playground_id, commit_id=prepared.commit_id, no_op=False
    )


def _create_from_prepared(connection, playground_id, prepared, base_version_id):
    """Create the Playground row for the first Version ever published to it.

    protocol.md §2 step 0: an unknown id is not an error, it is a Playground that
    does not exist until this moment. The row and the Version are accepted in one
    transaction, so there is no window in which a Playground exists without the
    Version that brought it into being.

    The primary key is the compare-and-swap here. Two publishes of the same new
    id cannot both insert — `idempotency.execute` runs this inside one immediate
    transaction, so the second one reads the row the first wrote, finds it does
    not match the (empty) base it sent, and is refused as ``conflict``. Nothing
    is overwritten and nothing is retried.
    """

    if base_version_id:
        raise PlaygroundBaseChangedError("playground has no history to base on")

    connection.execute(
        "INSERT INTO playgrounds (id, created_at, state, current_version_id)"
        " VALUES (?, ?, 'active', ?)",
        (playground_id, _now(), prepared.commit_id),
    )
    if not prepared.no_op:
        record_publish_event(
            connection,
            playground_id,
            prepared.commit_id,
            event_id=prepared.event_id,
        )
    return PublishResult(
        playground_id=playground_id,
        commit_id=prepared.commit_id,
        no_op=prepared.no_op,
    )


def publish_playground(
    connection, playground_id, repository, bundle, metadata, base_version_id=None
):
    """The three phases in one call, for callers with no HTTP layer above them.

    The service does *not* use this: it sequences the phases itself so the Git
    work sits outside the database transaction (a Git subprocess inside
    ``BEGIN IMMEDIATE`` holds the write lock for its whole duration). This
    exists for tests and for anything that wants the whole operation, and it
    keeps the phases in the order protocol.md §2 requires.
    """

    require_publishable(connection, playground_id, base_version_id)
    event_id = uuid4().hex
    prepared = _repository.publish_playground_prepare(
        repository, playground_id, bundle, metadata, event_id
    )

    try:
        with database.immediate_transaction(connection):
            result = commit_prepared_version(
                connection, playground_id, prepared, base_version_id
            )
    except BaseException:
        if not prepared.no_op:
            _repository.delete_pending_ref(repository, event_id)
        raise

    if not prepared.no_op:
        _repository.converge_history_ref(repository, playground_id, result.commit_id)
        _repository.delete_pending_ref(repository, event_id)
    return result


def recover_pending_refs(connection, repository):
    """Bring every pending ref to a settled state, at start-up.

    protocol.md §2's crash recovery. A pending ref means a publish was
    interrupted somewhere between "the commit exists" and "the database knows":
    either the transaction never committed, in which case the Version was never
    accepted and the ref is dropped, or it did commit, in which case the history
    ref is made to point at what the database says is current and the pending ref
    is dropped.

    The database is the only thing consulted for what is accepted — this never
    replays a publish. Idempotent by construction: it only ever removes refs and
    converges one ref to a commit, all of which are no-ops the second time.

    Returns ``(dropped, converged)``: how many pending refs were discarded and
    how many were settled against an accepted Event.
    """

    dropped = 0
    converged = 0
    for event_id, _commit_id in _repository.list_pending_refs(repository):
        payload = events.find_accepted_event(
            connection, event_id, events_types.EVENT_KIND_PUBLISH_PLAYGROUND
        )
        playground_id = (payload or {}).get("playground_id")
        if type(playground_id) is str and playground_id:
            row = _playground_row(connection, playground_id)
            # Converge to what the *database* says is current, never to the
            # commit the pending ref points at: a later publish may already
            # have moved on, and the ref must not be pulled backwards.
            if row is not None and row["current_version_id"]:
                _repository.converge_history_ref(
                    repository, row["id"], row["current_version_id"]
                )
                converged += 1
        _repository.delete_pending_ref(repository, event_id)
        dropped += 1
    return dropped, converged


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


def migrate_legacy_refs(connection, repository):
    """Move every Playground's history into the namespaced ref layout.

    Reads the rows rather than the refs: a Playground with no row is not part of
    the catalog, and its ref (if any) is not this service's to move.

    Returns how many were migrated.
    """

    rows = connection.execute("SELECT id FROM playgrounds ORDER BY id").fetchall()
    return _repository.migrate_legacy_refs(
        repository, [row["id"] for row in rows]
    )


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


def record_publish_event(connection, playground_id, version_id, event_id=None):
    """Record a server-generated ``publish_playground`` event.

    Writes the event row, appends to the content log and updates the
    playground's ``current_version_id``. Runs in the caller's transaction
    and never commits, so the event lands only if the publish that produced
    it does too.

    ``event_id`` is supplied by a publish, because the pending ref that protects
    the prepared Version is named after it and recovery is what matches the two
    up. Without one a fresh id is generated (archive and restore have no pending
    ref to name).
    """
    envelope = events_types.EventEnvelope(
        event_id=event_id or uuid4().hex,
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
