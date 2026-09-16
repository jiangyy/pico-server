"""Immutable Submission operations for PicoPG.

A Submission records that one Student's exact, Server-owned Work commit was
submitted for one Release.  Submissions are append-only: this module exposes no
update, delete, or status behavior, stores no source or mutable state, and
never copies Work content.  Creation re-reads current Class membership and the
exact Release/Work relationship on every call and then records the row through
the task-10 persistent idempotency layer, so an exact retry replays the
original result and a changed body conflicts.

The centralized content authorization reads the real ``submissions`` table
through :func:`release_ids_for_work_commit`; a Teacher is authorized for a Work
commit only when a real Submission of that exact commit exists under a Release
in a Class they own.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import accounts
import idempotency
import repository as repository_module


# One logical create scope: the request body names the Release and the Work
# commit, so reusing a key for a different Release or commit is a conflict.
SUBMISSION_CREATE_ENDPOINT = "/api/v1/submissions"

# Git object IDs are lowercase hexadecimal.  Requiring the exact full-width
# form before any lookup stops a symbolic name such as ``HEAD`` from being
# resolved as if it were the supplied Work commit.
_COMMIT_ID_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z|\A[0-9a-f]{64}\Z")


class SubmissionError(Exception):
    """Base class for Submission domain failures."""


class InvalidSubmissionRequestError(SubmissionError, ValueError):
    """A Submission request carried a malformed field or time value."""


class UnknownReleaseError(SubmissionError):
    """The target Release does not exist."""


class SubmissionNotAllowedError(SubmissionError):
    """The Release is not open, or a required Submission is past due."""


class UnknownWorkCommitError(SubmissionError):
    """The commit is not on the Student's Work history or pins another."""


class MissingSubmissionError(SubmissionError):
    """No readable Submission exists for the requested identifier."""


@dataclass(frozen=True)
class Submission:
    """Immutable metadata for one Student/Release/Work-commit record."""

    id: str
    release_id: str
    student_id: str
    student_username: str
    work_commit_id: str
    created_at: str


def submission_to_mapping(submission):
    """Return the canonical camelCase mapping for a Submission.

    ``username`` exposes the submitting Student's account name so a Teacher
    reading a Submission list can identify the Student without a second
    account lookup.  It is never Work source content.
    """
    return {
        "id": submission.id,
        "releaseId": submission.release_id,
        "studentId": submission.student_id,
        "username": submission.student_username,
        "workCommitId": submission.work_commit_id,
        "createdAt": submission.created_at,
    }


# --- validation helpers ----------------------------------------------------


def _format_time(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _now():
    return _format_time(datetime.now(timezone.utc))


def _normalize_now(value):
    """Return a canonical UTC RFC3339 string for the boundary comparison."""

    if value is None:
        return _now()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidSubmissionRequestError(
                "now must include a timezone"
            )
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise InvalidSubmissionRequestError("now must not be empty")
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as error:
            raise InvalidSubmissionRequestError(
                "now must be an RFC3339 timestamp"
            ) from error
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise InvalidSubmissionRequestError(
                "now must include a timezone"
            )
    else:
        raise InvalidSubmissionRequestError(
            "now must be a timestamp or RFC3339 string"
        )
    return _format_time(moment.astimezone(timezone.utc))


def _require_student(actor):
    if not isinstance(actor, accounts.User):
        raise accounts.ForbiddenActionError("valid account required")
    if actor.role != "student":
        raise accounts.ForbiddenActionError("student role required")
    return actor


def _require_actor(actor):
    if not isinstance(actor, accounts.User):
        raise accounts.ForbiddenActionError("valid account required")
    return actor


def _require_identifier(value, field):
    if not isinstance(value, str) or not value:
        raise InvalidSubmissionRequestError(
            f"{field} must be a non-empty string"
        )
    return value


def _require_commit_id(value):
    value = _require_identifier(value, "workCommitId")
    if _COMMIT_ID_PATTERN.match(value) is None:
        raise InvalidSubmissionRequestError(
            "workCommitId must be a Git object id"
        )
    return value


def _require_repository(repository):
    if not isinstance(repository, repository_module.Repository):
        raise InvalidSubmissionRequestError("repository adapter required")
    return repository


# --- database lookups ------------------------------------------------------


def _require_release(connection, release_id):
    row = connection.execute(
        "SELECT * FROM releases WHERE id = ?", (release_id,)
    ).fetchone()
    if row is None:
        raise UnknownReleaseError(release_id)
    return row


def _current_class_id(connection, student_id):
    row = connection.execute(
        "SELECT class_id FROM class_memberships WHERE student_id = ?",
        (student_id,),
    ).fetchone()
    return None if row is None else row["class_id"]


def release_ids_for_work_commit(connection, student_id, work_commit_id):
    """Return the Release ids of one Student's Submissions of one Work commit.

    This is the narrow lookup used by the centralized content authorization.
    It reads only the real ``submissions`` table; an unknown or malformed input
    yields an empty list, so the caller fails closed when no row matches.
    """

    if not isinstance(student_id, str) or not student_id:
        return []
    if not isinstance(work_commit_id, str) or not work_commit_id:
        return []
    rows = connection.execute(
        """
        SELECT release_id FROM submissions
        WHERE student_id = ? AND work_commit_id = ?
        """,
        (student_id, work_commit_id),
    ).fetchall()
    return [row["release_id"] for row in rows]


def _submission_from_row(row):
    return Submission(
        id=row["id"],
        release_id=row["release_id"],
        student_id=row["student_id"],
        student_username=row["username"],
        work_commit_id=row["work_commit_id"],
        created_at=row["created_at"],
    )


# --- Work history and Server-owned metadata --------------------------------


def _work_commit_in_history(
    repository, playground_id, student_id, work_commit_id
):
    """Return whether the exact commit is reachable from the Work ref.

    Only the derived Work ref is used.  The Work ref is never resolved to its
    HEAD and substituted for the supplied commit; ``merge-base --is-ancestor``
    answers whether the exact object is part of that history.
    """

    try:
        ref = repository.work_ref(playground_id, student_id)
    except repository_module.RepositoryError:
        return False
    completed = repository_module._run_git(
        ("merge-base", "--is-ancestor", work_commit_id, ref),
        cwd=repository.path,
    )
    return completed.returncode == 0


def _work_pinned_playground_commit(repository, work_commit_id):
    """Return the Playground commit pinned by the Work commit, or ``None``.

    The Work domain owns the ``.picopg/work.json`` reader; it is imported here
    lazily so the Submission and Work modules do not form an import cycle.
    """

    import works

    try:
        return works._read_base_work_playground_commit_id(
            repository, work_commit_id
        )
    except (works.WorkError, repository_module.RepositoryError):
        return None


# --- creation --------------------------------------------------------------


def _authorize_membership(connection, student_id, release):
    if _current_class_id(connection, student_id) != release["class_id"]:
        raise accounts.ForbiddenActionError(
            "student is not a member of the release class"
        )


def _authorize_open(release, evaluated_at):
    if release["open_at"] > evaluated_at:
        raise SubmissionNotAllowedError("release is not open")


def _authorize_due(release, evaluated_at):
    if not release["requires_submission"] or release["due_at"] is None:
        return
    if evaluated_at > release["due_at"]:
        raise SubmissionNotAllowedError("submission is past due")


def _authorize_work_commit(repository, release, student_id, work_commit_id):
    if not _work_commit_in_history(
        repository, release["playground_id"], student_id, work_commit_id
    ):
        raise UnknownWorkCommitError(
            "work commit is not on the student's work history"
        )
    if (
        _work_pinned_playground_commit(repository, work_commit_id)
        != release["playground_commit_id"]
    ):
        raise UnknownWorkCommitError(
            "work commit pins a different playground commit"
        )


def _insert_submission(
    connection, release_id, student_id, student_username, work_commit_id
):
    submission = Submission(
        id=uuid.uuid4().hex,
        release_id=release_id,
        student_id=student_id,
        student_username=student_username,
        work_commit_id=work_commit_id,
        created_at=_now(),
    )
    connection.execute(
        """
        INSERT INTO submissions (
            id, release_id, student_id, work_commit_id, created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            submission.id,
            submission.release_id,
            submission.student_id,
            submission.work_commit_id,
            submission.created_at,
        ),
    )
    return submission


def create_submission(
    connection,
    actor,
    repository,
    release_id,
    work_commit_id,
    request_key,
    now=None,
):
    """Create one immutable Submission, idempotently, in a single transaction.

    Only the Student actor may submit, and only for their own Work.  The
    Student must currently be a member of the Release Class, the Release must
    be open, and a required Submission must not be past due.  The supplied
    commit must be on the Student's own Work history and its Server-owned
    metadata must pin the exact Playground commit fixed by the Release.  The
    row and the idempotency result commit or roll back together, so an exact
    retry returns the stored ``201`` body and a changed body raises
    :class:`idempotency.IdempotencyConflict`.
    """

    actor = _require_student(actor)
    release_id = _require_identifier(release_id, "releaseId")
    work_commit_id = _require_commit_id(work_commit_id)
    repository = _require_repository(repository)
    evaluated_at = _normalize_now(now)

    endpoint = SUBMISSION_CREATE_ENDPOINT
    request = {
        "releaseId": release_id,
        "workCommitId": work_commit_id,
    }

    def callback(business_connection):
        release = _require_release(business_connection, release_id)
        _authorize_membership(business_connection, actor.id, release)
        _authorize_open(release, evaluated_at)
        _authorize_due(release, evaluated_at)
        _authorize_work_commit(
            repository, release, actor.id, work_commit_id
        )
        submission = _insert_submission(
            business_connection,
            release_id,
            actor.id,
            actor.username,
            work_commit_id,
        )
        return 201, {"submission": submission_to_mapping(submission)}

    return idempotency.execute(
        connection, actor.id, endpoint, request_key, request, callback
    )


# --- reads -----------------------------------------------------------------


def list_submissions(connection, actor):
    """Return the actor-filtered list ordered by ``(created_at, id)``.

    A Student sees only their own records, a Teacher sees only records under
    Releases in Classes they own, and an Admin sees all Submission metadata.
    No role reveals Work source content.
    """

    _require_actor(actor)

    if actor.role == "admin":
        rows = connection.execute(
            """
            SELECT submissions.*, users.username AS username
            FROM submissions
            JOIN users ON users.id = submissions.student_id
            ORDER BY submissions.created_at, submissions.id
            """
        ).fetchall()
    elif actor.role == "teacher":
        rows = connection.execute(
            """
            SELECT submissions.*, users.username AS username
            FROM submissions
            JOIN users ON users.id = submissions.student_id
            JOIN releases ON releases.id = submissions.release_id
            JOIN classes ON classes.id = releases.class_id
            WHERE classes.teacher_id = ?
            ORDER BY submissions.created_at, submissions.id
            """,
            (actor.id,),
        ).fetchall()
    elif actor.role == "student":
        rows = connection.execute(
            """
            SELECT submissions.*, users.username AS username
            FROM submissions
            JOIN users ON users.id = submissions.student_id
            WHERE submissions.student_id = ?
            ORDER BY submissions.created_at, submissions.id
            """,
            (actor.id,),
        ).fetchall()
    else:
        raise accounts.ForbiddenActionError("unknown role")

    return [_submission_from_row(row) for row in rows]


def _row_is_visible(connection, actor, row):
    if actor.role == "admin":
        return True
    if actor.role == "teacher":
        owner = connection.execute(
            """
            SELECT classes.teacher_id
            FROM releases
            JOIN classes ON classes.id = releases.class_id
            WHERE releases.id = ?
            """,
            (row["release_id"],),
        ).fetchone()
        return owner is not None and owner["teacher_id"] == actor.id
    if actor.role == "student":
        return row["student_id"] == actor.id
    raise accounts.ForbiddenActionError("unknown role")


def get_submission(connection, actor, submission_id):
    """Return one visible Submission, hiding missing and unauthorized rows."""

    _require_actor(actor)
    submission_id = _require_identifier(submission_id, "submissionId")

    row = connection.execute(
        """
        SELECT submissions.*, users.username AS username
        FROM submissions
        JOIN users ON users.id = submissions.student_id
        WHERE submissions.id = ?
        """,
        (submission_id,),
    ).fetchone()
    if row is None or not _row_is_visible(connection, actor, row):
        raise MissingSubmissionError(submission_id)
    return _submission_from_row(row)
