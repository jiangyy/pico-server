"""Immutable Submission records for PicoPG.

A Submission is the whole write side of the service: the files a student hands
in, plus the name they hand them in under.  Nothing here builds a Git commit,
copies course material, or reads a blob — the client names the exact published
Playground commit the work belongs to, that commit is checked against the
Playground's own history, and only its id is stored.  A submission therefore
cannot alter what other clients download, and re-submitting the same work is a
new immutable row rather than an edit.

The file set is the one place a client contributes bytes, so this module also
owns the rule for what such a set may look like: only portable paths under
``work/``, bounded in count, per-file bytes and total bytes.  The portability
and collision rules are the shared ones in :mod:`bundles`, which is why a
submitted path cannot be one a bundle would refuse.

Submissions are append-only: this module exposes no update, delete, or status
behavior.  Creation goes through the persistent idempotency layer, so an exact
retry returns the original ``201`` body and a changed body conflicts.
"""

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import bundles
import idempotency
import permissions
import playgrounds
import repository as repository_module
from pagination import Page


# One logical create scope: the request names the Playground, the exact commit,
# the student, and the files, so reusing a key for a different body conflicts.
SUBMISSION_CREATE_ENDPOINT = "/api/v1/submissions"

# A self-reported student name is a display string, not an identity: it is
# bounded, trimmed, and never used for authorization.
MAX_STUDENT_LENGTH = 128

# Git object IDs are lowercase hexadecimal.  Requiring the exact full-width
# form before any lookup stops a symbolic name such as ``HEAD`` from being
# resolved as if it were the supplied commit.
_COMMIT_ID_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z|\A[0-9a-f]{64}\Z")

# The only prefix a client may contribute under: everything else in a commit
# (``course/**``, ``starter/**``, ``picopg.json``) belongs to the publisher.
WORK_PREFIX = "work/"


class SubmissionError(Exception):
    """Base class for Submission domain failures."""


class InvalidSubmissionRequestError(SubmissionError, ValueError):
    """A Submission request carried a malformed field."""


class UnknownPlaygroundError(SubmissionError):
    """The named Playground has no such published commit."""


class MissingSubmissionError(SubmissionError):
    """No readable Submission exists for the requested identifier."""


class StoredSubmissionError(SubmissionError):
    """A durable Submission row cannot be represented by the API."""


@dataclass(frozen=True)
class SubmissionRequest:
    """One validated, bounded submission body."""

    playground_id: str
    playground_commit_id: str
    student: str
    """Canonical ``work/**`` path to text, sorted; the files handed in."""
    files: Mapping[str, str]


@dataclass(frozen=True)
class Submission:
    """One stored Submission without its files."""

    id: str
    playground_id: str
    playground_commit_id: str
    student: str
    created_at: str


@dataclass(frozen=True)
class StoredSubmission:
    """One stored Submission with the exact files that were handed in."""

    submission: Submission
    files: tuple


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_playground_id(value):
    if not repository_module.valid_playground_id(value):
        raise InvalidSubmissionRequestError("playgroundId is not valid")
    return value


def _require_commit_id(value):
    if type(value) is not str or _COMMIT_ID_PATTERN.match(value) is None:
        raise InvalidSubmissionRequestError("commitId is not a commit id")
    return value


def _require_student(value):
    if type(value) is not str:
        raise InvalidSubmissionRequestError("student must be a string")
    student = value.strip()
    if not student or len(student) > MAX_STUDENT_LENGTH:
        raise InvalidSubmissionRequestError("student must be 1..128 characters")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in student):
        raise InvalidSubmissionRequestError("student must not carry control characters")
    return student


def _normalized_work_path(path):
    """Return the canonical ``work/**`` path, or raise the reason it cannot be.

    The segment rules are :func:`bundles.portable_segment_reason`, so a path a
    bundle would refuse is refused here too, with the same reason code.  Only
    the ``work/`` prefix is this module's own rule.
    """

    if type(path) is not str:
        raise InvalidSubmissionRequestError("invalid files: path_not_str")

    normalized = unicodedata.normalize("NFC", path)
    if normalized.startswith("/"):
        raise InvalidSubmissionRequestError("invalid files: path_absolute")

    segments = normalized.split("/")
    for segment in segments:
        reason = bundles.portable_segment_reason(segment)
        if reason is not None:
            raise InvalidSubmissionRequestError(f"invalid files: {reason}")

    if not normalized.startswith(WORK_PREFIX) or len(normalized) == len(WORK_PREFIX):
        raise InvalidSubmissionRequestError("invalid files: path_not_allowed")
    return normalized


def _require_files(value):
    """Validate the handed-in file set and return canonical ``path -> text``.

    Nothing is returned unless the whole set passes: the entry count and the
    per-file and total byte bounds are the bundle limits, and a path that
    normalizes onto another one is a refusal rather than a silent overwrite.
    """

    if type(value) is not dict:
        raise InvalidSubmissionRequestError("files must be an object of path to text")
    if len(value) > bundles.MAX_ENTRY_COUNT:
        raise InvalidSubmissionRequestError("invalid files: too_many_entries")

    normalized = {}
    total_bytes = 0
    for path, text in value.items():
        if type(text) is not str:
            raise InvalidSubmissionRequestError("invalid files: entry_data_not_bytes")
        canonical = _normalized_work_path(path)
        encoded = len(text.encode("utf-8"))
        if encoded > bundles.MAX_FILE_BYTES:
            raise InvalidSubmissionRequestError("invalid files: file_too_large")
        total_bytes += encoded
        if total_bytes > bundles.MAX_TOTAL_BYTES:
            raise InvalidSubmissionRequestError("invalid files: work_too_large")
        if canonical in normalized:
            raise InvalidSubmissionRequestError("invalid files: duplicate_path")
        normalized[canonical] = text

    if bundles.portable_path_collision(normalized):
        raise InvalidSubmissionRequestError("invalid files: path_case_collision")

    # Sorted for a deterministic stored order that does not depend on input.
    return MappingProxyType(dict(sorted(normalized.items())))


def decode_submission_request(payload):
    """Validate a submission body and return a normalized request.

    The playground id, the commit id and the student name are checked for the
    exact shape the API promises; the file set is checked by
    :func:`_require_files`.  Nothing is returned unless the whole body passes.
    """

    if type(payload) is not dict:
        raise InvalidSubmissionRequestError("body must be an object")

    return SubmissionRequest(
        playground_id=_require_playground_id(payload.get("playgroundId")),
        playground_commit_id=_require_commit_id(payload.get("commitId")),
        student=_require_student(payload.get("student")),
        files=_require_files(payload.get("files")),
    )


def _require_published_commit(connection, repository, request):
    """Fail unless the named commit is in that Playground's own history."""

    if playgrounds.get_playground(connection, request.playground_id) is None:
        raise UnknownPlaygroundError("playground does not exist")
    if not repository_module.commit_in_playground_history(
        repository, request.playground_id, request.playground_commit_id
    ):
        raise UnknownPlaygroundError("commit is not published")


def create_submission(connection, repository, request, request_key):
    """Create one immutable Submission, idempotently, in one transaction.

    The Playground row and the published-commit check run inside the same
    transaction as the row insert, so a submission can only ever reference
    content this server actually publishes.  The row and the idempotency result
    commit or roll back together, so an exact retry returns the stored ``201``
    body and a changed body raises
    :class:`idempotency.IdempotencyConflict`.
    """

    if type(request) is not SubmissionRequest:
        raise InvalidSubmissionRequestError("a decoded request is required")

    logical_request = {
        "playgroundId": request.playground_id,
        "commitId": request.playground_commit_id,
        "student": request.student,
        "files": dict(request.files),
    }

    def callback(business_connection):
        _require_published_commit(business_connection, repository, request)

        submission_id = uuid.uuid4().hex
        created_at = _now()
        business_connection.execute(
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
                json.dumps(dict(request.files), ensure_ascii=False, sort_keys=True),
                created_at,
            ),
        )
        stored = Submission(
            id=submission_id,
            playground_id=request.playground_id,
            playground_commit_id=request.playground_commit_id,
            student=request.student,
            created_at=created_at,
        )
        return 201, {"submission": submission_to_mapping(stored)}

    status, response = idempotency.execute(
        connection,
        request.student,
        SUBMISSION_CREATE_ENDPOINT,
        request_key,
        logical_request,
        callback,
    )
    return status, response


# --- the copy a human opens ------------------------------------------------
#
# The database row is the record; these files are for whoever marks the work.
# One submission lands at::
#
#     <root>/<班级>/<姓名>/<YYYYMMDD-HHMMSS>-<id8>/work/main.py
#     <root>/<班级>/<姓名>/<YYYYMMDD-HHMMSS>-<id8>/submission.json
#
# A name with no 班级 is filed directly under ``<root>/<姓名>/``.
#
# The student string is the only caller-controlled part of that path, so it is
# sanitized into safe single segments; the file paths below it are already
# portable ``work/**`` (:func:`_normalized_work_path`), so joining them cannot
# escape the submission directory.  Writing the same submission twice writes
# the same bytes to the same path, which is what makes a replayed idempotent
# retry harmless.

# A name is a label, not a payload: 64 characters is more than any real one and
# keeps the whole path inside the usual filesystem limits.
MAX_SEGMENT_LENGTH = 64

# The client files work under one display name, ``姓名（班级）``.  Splitting it
# is presentation, not identity: a name that does not match is used as-is.
_STUDENT_PATTERN = re.compile(r"\A(.+?)（(.+?)）\Z")

MANIFEST_NAME = "submission.json"


def _safe_segment(value):
    """Return ``value`` as one filesystem-safe path segment (possibly empty)."""

    normalized = unicodedata.normalize("NFC", value)
    printable = "".join(
        " " if character in "/\\\u0000" else character
        for character in normalized
        if character.isprintable() or character == "\u0000"
    )
    return " ".join(printable.split()).strip(". ")[:MAX_SEGMENT_LENGTH]


def student_segments(student):
    """Return ``(class_room, name)`` for a self-reported name.

    ``张三（1 班）`` becomes ``("1 班", "张三")``; anything else is a bare name
    with no class.  Both parts are sanitized, and an empty result falls back to
    the whole string so a submission never lands in an unnamed directory.
    """

    match = _STUDENT_PATTERN.match(student)
    if match is not None:
        name = _safe_segment(match.group(1))
        class_room = _safe_segment(match.group(2))
        if name and class_room:
            return class_room, name
    return "", _safe_segment(student) or "student"


def submission_directory(root, submission_id, student, created_at):
    """Return the directory one submission's files belong in, as a path.

    The timestamp keeps folders in the order the work arrived; the id suffix
    keeps two submissions in the same second apart.
    """

    digits = "".join(character for character in created_at if character.isdigit())
    stamp = f"{digits[:8]}-{digits[8:14]}" if len(digits) >= 14 else digits
    class_room, name = student_segments(student)
    parts = [part for part in (class_room, name, f"{stamp}-{submission_id[:8]}") if part]

    directory = Path(root)
    for part in parts:
        directory = directory / part
    return directory


def write_submission_files(
    root,
    submission_id,
    student,
    playground_id,
    playground_commit_id,
    created_at,
    files,
):
    """Write one submission's files under ``root`` and return the directory.

    Nothing here decides whether a submission is valid: the caller has already
    stored the row, so these bytes are a copy of an accepted submission.  A
    failure raises, because a record without its files is exactly the state
    this function exists to prevent.
    """

    directory = permissions.ensure_private_directory(
        submission_directory(root, submission_id, student, created_at)
    )

    for path, text in files.items():
        target = directory.joinpath(*path.split("/"))
        permissions.ensure_private_directory(target.parent)
        target.write_text(text, encoding="utf-8")
        permissions.ensure_private_file(target)

    manifest = {
        "id": submission_id,
        "playgroundId": playground_id,
        "commitId": playground_commit_id,
        "student": student,
        "createdAt": created_at,
        "files": sorted(files),
    }
    manifest_path = directory / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    permissions.ensure_private_file(manifest_path)
    return directory


def _submission_from_row(row):
    """Return the Submission a durable row describes, or fail closed."""

    try:
        submission_id = row["id"]
        playground_id = row["playground_id"]
        playground_commit_id = row["playground_commit_id"]
        student = row["student"]
        created_at = row["created_at"]
    except (IndexError, KeyError) as error:
        raise StoredSubmissionError("stored submission is invalid") from error

    if (
        type(submission_id) is not str
        or not submission_id
        or not repository_module.valid_playground_id(playground_id)
        or type(playground_commit_id) is not str
        or _COMMIT_ID_PATTERN.match(playground_commit_id) is None
        or type(student) is not str
        or not student
        or type(created_at) is not str
        or not created_at
    ):
        raise StoredSubmissionError("stored submission is invalid")

    return Submission(
        id=submission_id,
        playground_id=playground_id,
        playground_commit_id=playground_commit_id,
        student=student,
        created_at=created_at,
    )


def get_submission(connection, submission_id):
    """Return one Submission with its files, or raise ``MissingSubmissionError``."""

    if type(submission_id) is not str or not submission_id:
        raise MissingSubmissionError("submission does not exist")

    row = connection.execute(
        "SELECT id, playground_id, playground_commit_id, student,"
        " files_json, created_at FROM submissions WHERE id = ?",
        (submission_id,),
    ).fetchone()
    if row is None:
        raise MissingSubmissionError("submission does not exist")

    submission = _submission_from_row(row)
    try:
        files = json.loads(row["files_json"])
    except (TypeError, ValueError) as error:
        raise StoredSubmissionError("stored submission is invalid") from error
    if type(files) is not dict or any(
        type(path) is not str or type(text) is not str
        for path, text in files.items()
    ):
        raise StoredSubmissionError("stored submission is invalid")

    return StoredSubmission(
        submission=submission,
        files=tuple(sorted(files.items())),
    )


def list_submissions(connection, *, limit, after=None):
    """Return one keyset page of submissions, newest first.

    The bound is applied at the authoritative query with ``LIMIT limit + 1``,
    so a page never grows past the caller's limit.  A durable row that cannot
    be represented fails closed instead of being silently skipped.
    """

    if type(limit) is not int or limit <= 0:
        raise InvalidSubmissionRequestError("limit must be positive")

    after_key = None
    if after is not None:
        after_key = after if isinstance(after, tuple) else tuple(after)
        if len(after_key) != 2:
            raise InvalidSubmissionRequestError("cursor is not valid")

    if after_key is None:
        rows = connection.execute(
            "SELECT id, playground_id, playground_commit_id, student,"
            " files_json, created_at FROM submissions"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit + 1,),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT id, playground_id, playground_commit_id, student,"
            " files_json, created_at FROM submissions"
            " WHERE (created_at, id) < (?, ?)"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (after_key[0], after_key[1], limit + 1),
        ).fetchall()

    items = tuple(_submission_from_row(row) for row in rows[:limit])
    next_key = None
    if len(rows) > limit:
        next_key = (items[-1].created_at, items[-1].id)
    return Page(items=items, next_key=next_key)


def submission_to_mapping(submission, files=None):
    """Return the bounded JSON view of one Submission."""

    if type(submission) is not Submission:
        raise StoredSubmissionError("stored submission is invalid")

    mapping = {
        "id": submission.id,
        "playgroundId": submission.playground_id,
        "commitId": submission.playground_commit_id,
        "student": submission.student,
        "createdAt": submission.created_at,
    }
    if files is not None:
        mapping["files"] = [
            {"path": path, "text": text} for path, text in files
        ]
    return mapping
