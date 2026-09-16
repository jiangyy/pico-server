"""Central content read authorization for PicoPG.

Every decision about reading a Playground commit or a student Work is made by
:func:`authorize_content_read`; callers never assemble their own policy checks.
Missing records and unauthorized actors receive the exact same denial, so the
existence of a Playground, Class, Release, Work, or Submission cannot be probed.

The database and the private Git repository remain the only authorities.  Exact
record identifiers, current Class membership, and the requested commit are
re-read on every call; nothing is cached and no role is used as a shortcut for
the rules below.

No HTTP, content output, role shortcut, or authorization cache is involved.
"""

import json
import re
from datetime import datetime, timezone

import accounts
import playgrounds
import repository as repository_module
import submissions


__all__ = [
    "ContentAccessDeniedError",
    "InvalidAuthorizationRequestError",
    "authorize_content_read",
]


# Git object IDs are lowercase hexadecimal.  Rejecting anything else before a
# Git subprocess runs also stops a symbolic name such as ``HEAD`` from being
# resolved as if it were a commit ID.
_COMMIT_ID_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z|\A[0-9a-f]{64}\Z")

# Server-owned linkage written into every Work commit.  The file is produced by
# the Server, so its ``playgroundCommitId`` is trusted as the pinned Playground
# version of that Work.
_WORK_MANIFEST_PATH = ".picopg/work.json"


class InvalidAuthorizationRequestError(ValueError):
    """A caller supplied a malformed repository handle or time value."""


class ContentAccessDeniedError(Exception):
    """Raised for every missing, unknown, or unauthorized content target.

    The message never names the target, so a caller cannot distinguish a
    record that does not exist from one the actor may not read.
    """


def _deny():
    return ContentAccessDeniedError("content is not readable")


def _require_actor(actor):
    if not isinstance(actor, accounts.User):
        raise _deny()
    return actor


def _require_identifier(value):
    if not isinstance(value, str) or not value:
        raise _deny()
    return value


def _require_commit_id(value):
    value = _require_identifier(value)
    if _COMMIT_ID_PATTERN.match(value) is None:
        raise _deny()
    return value


def _require_repository(repository):
    if not isinstance(repository, repository_module.Repository):
        raise InvalidAuthorizationRequestError("repository handle required")
    return repository


def _normalize_now(now):
    """Return a canonical UTC RFC3339 string for the open-time comparison."""

    if now is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise InvalidAuthorizationRequestError(
                "now must include a timezone"
            )
        moment = now
    elif isinstance(now, str):
        text = now.strip()
        if not text:
            raise InvalidAuthorizationRequestError("now must not be empty")
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as error:
            raise InvalidAuthorizationRequestError(
                "now must be an RFC3339 timestamp"
            ) from error
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise InvalidAuthorizationRequestError(
                "now must include a timezone"
            )
    else:
        raise InvalidAuthorizationRequestError(
            "now must be a timestamp or RFC3339 string"
        )
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


# --- Git lookups -----------------------------------------------------------
#
# Authorization needs to prove that the requested commit really belongs to the
# stated Playground or Work and to read the Server-owned Work manifest.  The
# repository module owns the sanitized Git environment, so these read-only
# helpers reuse its subprocess bridge instead of starting Git directly.


def _resolve_ref(repository, ref):
    completed = repository_module._run_git(
        ("rev-parse", "--verify", "--quiet", ref), cwd=repository.path
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip().decode("ascii", "replace")
    return value or None


def _is_ancestor(repository, commit_id, ref):
    completed = repository_module._run_git(
        ("merge-base", "--is-ancestor", commit_id, ref), cwd=repository.path
    )
    return completed.returncode == 0


def _commit_in_ref_history(repository, ref, commit_id):
    if _resolve_ref(repository, ref) is None:
        return False
    return _is_ancestor(repository, commit_id, ref)


def _commit_in_playground_history(repository, playground_id, commit_id):
    try:
        ref = repository.playground_ref(playground_id)
        return _commit_in_ref_history(repository, ref, commit_id)
    except repository_module.RepositoryError:
        return False


def playground_commit_is_published(repository, playground_id, commit_id):
    """Return whether ``commit_id`` belongs to the named Playground history.

    This is a read-only Git check used by the Work content service.  It makes
    no actor decision; authorization is decided separately by
    :func:`authorize_content_read`.
    """

    return _commit_in_playground_history(repository, playground_id, commit_id)


def _commit_in_work_history(repository, playground_id, student_id, commit_id):
    try:
        ref = repository.work_ref(playground_id, student_id)
        return _commit_in_ref_history(repository, ref, commit_id)
    except repository_module.RepositoryError:
        return False


def _work_manifest_commit_id(repository, ref):
    """Return the pinned Playground commit from a Work HEAD, or ``None``."""

    try:
        completed = repository_module._run_git(
            ("show", f"{ref}:{_WORK_MANIFEST_PATH}"), cwd=repository.path
        )
    except repository_module.RepositoryError:
        return None
    if completed.returncode != 0:
        return None
    try:
        manifest = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    value = manifest.get("playgroundCommitId")
    if not isinstance(value, str) or _COMMIT_ID_PATTERN.match(value) is None:
        return None
    return value


def _own_work_references_commit(repository, playground_id, student_id, commit_id):
    """Check the Server-owned manifest of the student's own Work HEAD."""

    try:
        ref = repository.work_ref(playground_id, student_id)
    except repository_module.RepositoryError:
        return False
    if _resolve_ref(repository, ref) is None:
        return False
    return _work_manifest_commit_id(repository, ref) == commit_id


# --- Database lookups ------------------------------------------------------


def _playground_row(connection, playground_id):
    return connection.execute(
        "SELECT id, creator_id, policy, archived_at"
        " FROM playgrounds WHERE id = ?",
        (playground_id,),
    ).fetchone()


def _current_class_id(connection, student_id):
    row = connection.execute(
        "SELECT class_id FROM class_memberships WHERE student_id = ?",
        (student_id,),
    ).fetchone()
    return None if row is None else row["class_id"]


def _class_has_open_release(
    connection, class_id, playground_id, commit_id, visible_through
):
    row = connection.execute(
        """
        SELECT 1 FROM releases
        WHERE class_id = ?
          AND playground_id = ?
          AND playground_commit_id = ?
          AND open_at <= ?
        LIMIT 1
        """,
        (class_id, playground_id, commit_id, visible_through),
    ).fetchone()
    return row is not None


def _teacher_owns_class_with_release(
    connection, teacher_id, playground_id, commit_id
):
    row = connection.execute(
        """
        SELECT 1 FROM releases
        JOIN classes ON classes.id = releases.class_id
        WHERE classes.teacher_id = ?
          AND releases.playground_id = ?
          AND releases.playground_commit_id = ?
        LIMIT 1
        """,
        (teacher_id, playground_id, commit_id),
    ).fetchone()
    return row is not None


def _teacher_can_read_submitted_work(
    connection, teacher_id, playground_id, student_id, commit_id
):
    # The lookup reads the real ``submissions`` table.  An unknown actor or
    # commit yields no rows, and a lookup failure fails closed, so a Teacher is
    # authorized only by a real Submission of this exact Work commit.
    try:
        release_ids = submissions.release_ids_for_work_commit(
            connection, student_id, commit_id
        )
    except Exception:
        return False
    if not release_ids:
        return False
    for release_id in release_ids:
        if not isinstance(release_id, str) or not release_id:
            continue
        row = connection.execute(
            """
            SELECT releases.playground_id, classes.teacher_id
            FROM releases
            JOIN classes ON classes.id = releases.class_id
            WHERE releases.id = ?
            """,
            (release_id,),
        ).fetchone()
        if row is None:
            continue
        if (
            row["teacher_id"] == teacher_id
            and row["playground_id"] == playground_id
        ):
            return True
    return False


def _authorize_playground_commit_read(
    connection, actor, repository, playground_id, commit_id, visible_through
):
    row = _playground_row(connection, playground_id)
    if row is None:
        raise _deny()

    # A Playground decision may only expose a commit that really is part of that
    # Playground's history.  This keeps a Work commit ID from being smuggled
    # through the Playground path, especially for public or Admin reads.
    if not _commit_in_playground_history(repository, playground_id, commit_id):
        raise _deny()

    if actor.role == "admin":
        return

    if (
        row["archived_at"] is None
        and row["policy"] == playgrounds.PUBLIC_POLICY
    ):
        return

    if actor.role == "student":
        current_class_id = _current_class_id(connection, actor.id)
        if current_class_id is not None and _class_has_open_release(
            connection,
            current_class_id,
            playground_id,
            commit_id,
            visible_through,
        ):
            return
        if _own_work_references_commit(
            repository, playground_id, actor.id, commit_id
        ):
            return
        raise _deny()

    if actor.role == "teacher":
        if _teacher_owns_class_with_release(
            connection, actor.id, playground_id, commit_id
        ):
            return
        raise _deny()

    raise _deny()


def _authorize_work_read(
    connection,
    actor,
    repository,
    playground_id,
    student_id,
    commit_id,
):
    # The commit must belong to the named student's Work history; an arbitrary
    # commit ID never authorizes anything on its own.
    if not _commit_in_work_history(
        repository, playground_id, student_id, commit_id
    ):
        raise _deny()

    if actor.role == "student":
        if actor.id == student_id:
            return
        raise _deny()

    if actor.role == "teacher":
        if _teacher_can_read_submitted_work(
            connection, actor.id, playground_id, student_id, commit_id
        ):
            return
        raise _deny()

    # Admin has no default access to student source.
    raise _deny()


def authorize_content_read(
    connection,
    actor,
    repository,
    playground_id,
    commit_id,
    *,
    work_student_id=None,
    now=None,
):
    """Decide whether ``actor`` may read one content commit.

    ``work_student_id`` selects a student Work owned by that Student; when it
    is ``None`` the target is a Playground commit.  ``now`` is only used for
    Playground open-time checks and defaults to the current UTC time.  A
    Teacher's Work access is decided by the real Submission lookup in
    :mod:`submissions`; there is no injected or default-deny hook.

    Returns ``None`` when access is allowed.  Every missing, unknown, or
    unauthorized target raises :class:`ContentAccessDeniedError`.
    """

    actor = _require_actor(actor)
    playground_id = _require_identifier(playground_id)
    commit_id = _require_commit_id(commit_id)
    repository = _require_repository(repository)

    if work_student_id is not None:
        student_id = _require_identifier(work_student_id)
        _authorize_work_read(
            connection,
            actor,
            repository,
            playground_id,
            student_id,
            commit_id,
        )
    else:
        _authorize_playground_commit_read(
            connection,
            actor,
            repository,
            playground_id,
            commit_id,
            _normalize_now(now),
        )
