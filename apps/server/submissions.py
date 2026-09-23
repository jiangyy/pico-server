"""Immutable Submission records for PicoPG.

A Submission is the whole write side of the service: the files a student hands
in, plus the name they hand them in under.  Nothing here builds a Git commit,
copies course material, or reads a blob — the client names the exact published
Assignment version the work answers, that version is checked against the
Assignment's own history, and only its id is stored.  A submission therefore
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import bundles
import permissions
import repository as repository_module
from pagination import Page


# A self-reported student name is a display string, not an identity: it is
# bounded, trimmed, and never used for authorization.
MAX_STUDENT_LENGTH = 128

# One directory name may be this long. Long enough for a class and a name,
# short enough that a path built from client-supplied text stays inside what a
# filesystem accepts.
MAX_SEGMENT_LENGTH = 64

# One delivery attempt may be named this long. The client generates the id, so
# this is a bound on what a caller can make the Server store, not a format.
MAX_SUBMISSION_ID_LENGTH = 64

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


class MissingSubmissionError(SubmissionError):
    """No readable Submission exists for the requested identifier."""


class StoredSubmissionError(SubmissionError):
    """A durable Submission row cannot be represented by the API."""


@dataclass(frozen=True)
class SubmissionDelivery:
    """One validated hand-in, as a client sends it.

    The client names the delivery (``submission_id``): that name is what makes a
    lost response retryable without producing a second Submission, so it travels
    with the request instead of being minted here. The Assignment version is the
    question being answered, so it is stored with the work and never re-derived.
    ``student`` is not sent at all — it is read out of the handed-in file's own
    header.

    Mutually consistent by construction: the field shapes, the file set and the
    derived student were all checked before this value existed.
    """

    submission_id: str
    assignment_id: str
    version_id: str
    student: str
    """Canonical ``work/**`` path to text, sorted; the files handed in."""
    files: Mapping[str, str]


@dataclass(frozen=True)
class SubmissionWrite:
    """What one delivery attempt did to the store.

    ``accepted``       — a new Submission was written.
    ``existing_same``  — this ``submission_id`` was already stored with exactly
                         this content; the original result is the answer.
    ``conflict``       — this ``submission_id`` is stored with *different*
                         content, which means two different hand-ins share one
                         name. Nothing is written, and the caller must not retry.
    """

    status: str
    submission_id: str
    created_at: str


@dataclass(frozen=True)
class Submission:
    """One stored Submission without its files."""

    id: str
    assignment_id: str
    version_id: str
    student: str
    created_at: str


@dataclass(frozen=True)
class StoredSubmission:
    """One stored Submission with the exact files that were handed in."""

    submission: Submission
    files: tuple


def _require_assignment_id(value):
    if not repository_module.valid_playground_id(value):
        raise InvalidSubmissionRequestError("assignment_id is not valid")
    return value


def _require_version_id(value):
    if type(value) is not str or _COMMIT_ID_PATTERN.match(value) is None:
        raise InvalidSubmissionRequestError("version_id is not a version id")
    return value


def _require_submission_id(value):
    if type(value) is not str:
        raise InvalidSubmissionRequestError("submission_id must be a string")
    if not value or len(value) > MAX_SUBMISSION_ID_LENGTH:
        raise InvalidSubmissionRequestError(
            f"submission_id must be 1..{MAX_SUBMISSION_ID_LENGTH} characters"
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise InvalidSubmissionRequestError("submission_id must not carry control characters")
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


def decode_delivery(payload):
    """Validate a hand-in and return the value the store will write.

    Every field is checked here before anything else looks at the request: the
    ids for their shape, the file set for its paths and bounds, and the student
    for being present and valid *in the files themselves* — a hand-in without a
    usable name is refused rather than filed under a guess.
    """

    if type(payload) is not dict:
        raise InvalidSubmissionRequestError("body must be an object")

    files = _require_files(payload.get("files"))
    student = student_from_files(files)
    if student is None:
        raise InvalidSubmissionRequestError("files carry no usable student header")

    return SubmissionDelivery(
        submission_id=_require_submission_id(payload.get("submission_id")),
        assignment_id=_require_assignment_id(payload.get("assignment_id")),
        version_id=_require_version_id(payload.get("version_id")),
        student=_require_student(student),
        files=files,
    )


# The one file a hand-in is required to carry its attribution in. The client
# writes the student's own header line there, so the handed-in bytes are the
# only copy of the name — there is no second one in the request.
ATTRIBUTION_PATH = "work/main.py"

#: The label pairs the header line knows, in the order they are documented.
_HEADER_LABELS = ("姓名", "班级")


def student_from_files(files):
    """Read the student name out of a hand-in's own header line.

    A submitted file set carries its attribution in ``work/main.py``: the
    first non-blank line is ``# 姓名：<name> 班级：<class>`` (the class part is
    optional). The returned value is the display name the record is filed
    under — ``<name>（<class>）`` when a class is present, ``<name>`` alone
    otherwise.

    Returns ``None`` when there is no usable header, so the caller can refuse
    the hand-in. Raises :class:`InvalidSubmissionRequestError` when a header
    is present but the name it produces is not a valid student string.
    """

    # Any mapping: the validator hands back a read-only view of the same set,
    # and reading one path out of it is all this does.
    if not isinstance(files, Mapping):
        return None
    text = files.get(ATTRIBUTION_PATH)
    if type(text) is not str:
        return None

    line = next((line for line in text.split("\n") if line.strip()), "")
    fields = _header_fields(line)
    name = fields.get("姓名", "")
    if not name:
        return None

    class_room = fields.get("班级", "")
    candidate = name if not class_room else f"{name}（{class_room}）"
    try:
        return _require_student(candidate)
    except InvalidSubmissionRequestError:
        return None


def _header_fields(line):
    """Every ``标签：值`` pair on a header line.

    A value ends at the next label marker of any kind, so ``班级`` before
    ``姓名`` reads the same as the documented order.
    """

    markers = tuple(f"{label}：" for label in _HEADER_LABELS)
    fields = {}
    for label in _HEADER_LABELS:
        marker = f"{label}："
        position = line.find(marker)
        if position < 0:
            continue
        rest = line[position + len(marker):]
        end = len(rest)
        for other in markers:
            other_position = rest.find(other)
            if 0 <= other_position < end:
                end = other_position
        fields[label] = rest[:end].strip()
    return fields


# --- The copy a teacher reads -------------------------------------------
#
# The database is the record; these folders are a projection of it, written so
# that a human can open a piece of work without going through the API. The
# student string is the only caller-controlled part of the path, so it is
# sanitized segment by segment: a name may not become a path.

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
    with no class. Both parts are sanitized, and an empty result falls back to
    the whole string so a submission never lands in an unnamed directory.
    """

    match = _STUDENT_PATTERN.match(student)
    if match is not None:
        name = _safe_segment(match.group(1))
        class_room = _safe_segment(match.group(2))
        if name and class_room:
            return class_room, name
    return "", _safe_segment(student) or "student"


def submission_ordinal(connection, student, submission_id, created_at):
    """Return this submission's 1-based position among that student's work.

    The number comes from the record, never from the filesystem: an idempotent
    replay must land in the same folder, and counting existing directories would
    hand it the next number instead. ``id`` breaks ties when two submissions
    share a timestamp.
    """

    row = connection.execute(
        """
        SELECT COUNT(*) FROM submissions
        WHERE student = ?
          AND (created_at < ? OR (created_at = ? AND id <= ?))
        """,
        (student, created_at, created_at, submission_id),
    ).fetchone()
    return int(row[0]) if row is not None else 1


def submission_directory(root, student, assignment_id, ordinal):
    """Return the directory one submission's files belong in, as a path.

    One folder per handed-in assignment: ``<班级>/<姓名>/<assignmentId>``, and
    ``<assignmentId>-2``, ``-3`` … for the same assignment handed in again.
    """

    folder = assignment_id if ordinal <= 1 else f"{assignment_id}-{ordinal}"
    class_room, name = student_segments(student)
    parts = [part for part in (class_room, name, _safe_segment(folder) or folder) if part]

    directory = Path(root)
    for part in parts:
        directory = directory / part
    return directory


def write_submission_files(
    root,
    submission_id,
    student,
    assignment_id,
    version_id,
    created_at,
    files,
    ordinal=1,
):
    """Write one submission's files under ``root`` and return the directory.

    Nothing here decides whether a submission is valid: the caller has already
    stored the row, so these bytes are a copy of an accepted submission. A
    failure raises, because a record whose folder is missing is exactly the
    state this function exists to prevent — and the caller's retry writes the
    same path, so the projection converges.
    """

    directory = permissions.ensure_private_directory(
        submission_directory(root, student, assignment_id, ordinal)
    )

    for path, text in files.items():
        target = directory.joinpath(*path.split("/"))
        permissions.ensure_private_directory(target.parent)
        target.write_text(text, encoding="utf-8")
        permissions.ensure_private_file(target)

    manifest = {
        "id": submission_id,
        "assignmentId": assignment_id,
        "versionId": version_id,
        "student": student,
        "createdAt": created_at,
        "ordinal": ordinal,
        "files": sorted(files),
    }
    manifest_path = directory / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    permissions.ensure_private_file(manifest_path)
    return directory


def backfill_submission_files(connection, root):
    """Write the readable copy of every stored submission that has none.

    The row is the record and the folder is a copy of it, so a copy can always
    be made again: this covers submissions stored before the projection existed,
    and a `submissions/` directory that was deleted or half-written. It runs
    once at start-up, like the Playground version backfill, rather than only
    when a submission is next handed in.

    Returns how many folders were written.
    """

    rows = connection.execute(
        "SELECT id, assignment_id, version_id, student, files_json, created_at"
        " FROM submissions ORDER BY created_at, id"
    ).fetchall()

    written = 0
    for row in rows:
        ordinal = submission_ordinal(
            connection, row["student"], row["id"], row["created_at"]
        )
        directory = submission_directory(
            root, row["student"], row["assignment_id"], ordinal
        )
        # The manifest is the marker that a folder is complete, so a half-written
        # one is repaired rather than skipped.
        if (directory / MANIFEST_NAME).is_file():
            continue
        try:
            files = json.loads(row["files_json"])
        except (TypeError, ValueError):
            continue
        if type(files) is not dict:
            continue
        write_submission_files(
            root,
            row["id"],
            row["student"],
            row["assignment_id"],
            row["version_id"],
            row["created_at"],
            files,
            ordinal,
        )
        written += 1
    return written


def create_if_absent(connection, delivery):
    """Store one Submission, or report what its ``submission_id`` already holds.

    The identity of a hand-in is the id the client sent, so "already here" is
    answered by that column alone: an id that exists with exactly this content is
    the same hand-in arriving twice (a retry after a lost response), and one that
    exists with different content is two hand-ins fighting over one name.

    The caller runs this inside one immediate transaction: the read and the write
    below must not interleave with another delivery of the same id, or both
    would find nothing and one would lose to the UNIQUE constraint.
    """

    files_json = json.dumps(dict(delivery.files), separators=(",", ":"), sort_keys=True)
    row = connection.execute(
        "SELECT id, assignment_id, version_id, student, files_json,"
        " created_at FROM submissions WHERE id = ?",
        (delivery.submission_id,),
    ).fetchone()

    if row is not None:
        same = (
            row["assignment_id"] == delivery.assignment_id
            and row["version_id"] == delivery.version_id
            and row["student"] == delivery.student
            and row["files_json"] == files_json
        )
        return SubmissionWrite(
            status="existing_same" if same else "conflict",
            submission_id=delivery.submission_id,
            created_at=row["created_at"],
        )

    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    connection.execute(
        """
        INSERT INTO submissions (
            id, assignment_id, version_id, student,
            files_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            delivery.submission_id,
            delivery.assignment_id,
            delivery.version_id,
            delivery.student,
            files_json,
            created_at,
        ),
    )
    return SubmissionWrite(
        status="accepted", submission_id=delivery.submission_id, created_at=created_at
    )


def _submission_from_row(row):
    """Return the Submission a durable row describes, or fail closed."""

    try:
        submission_id = row["id"]
        assignment_id = row["assignment_id"]
        version_id = row["version_id"]
        student = row["student"]
        created_at = row["created_at"]
    except (IndexError, KeyError) as error:
        raise StoredSubmissionError("stored submission is invalid") from error

    if (
        type(submission_id) is not str
        or not submission_id
        or not repository_module.valid_playground_id(assignment_id)
        or type(version_id) is not str
        or _COMMIT_ID_PATTERN.match(version_id) is None
        or type(student) is not str
        or not student
        or type(created_at) is not str
        or not created_at
    ):
        raise StoredSubmissionError("stored submission is invalid")

    return Submission(
        id=submission_id,
        assignment_id=assignment_id,
        version_id=version_id,
        student=student,
        created_at=created_at,
    )


def get_submission(connection, submission_id):
    """Return one Submission with its files, or raise ``MissingSubmissionError``."""

    if type(submission_id) is not str or not submission_id:
        raise MissingSubmissionError("submission does not exist")

    row = connection.execute(
        "SELECT id, assignment_id, version_id, student,"
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
            "SELECT id, assignment_id, version_id, student,"
            " files_json, created_at FROM submissions"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit + 1,),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT id, assignment_id, version_id, student,"
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
        "assignmentId": submission.assignment_id,
        "versionId": submission.version_id,
        "student": submission.student,
        "createdAt": submission.created_at,
    }
    if files is not None:
        mapping["files"] = [
            {"path": path, "text": text} for path, text in files
        ]
    return mapping
