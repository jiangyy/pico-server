"""Server-owned Work commit creation from a bounded ``work/**`` snapshot.

A Work commit is assembled entirely by the Server.  The caller supplies only a
normalized ``work/**`` snapshot; ``course/**``, ``starter/**`` and
``picopg.json`` are copied byte-for-byte from one exact Playground commit, and
``.picopg/work.json`` is written by the Server with the exact pinned
``playgroundCommitId``.  No client value selects the Work ref, the parent, or
the pinned Playground commit.

Authorization runs before the Playground tree is read: the actor must be the
Student who owns the Work, and that Student must be allowed to read the exact
Playground commit.  Every denial uses the one authorization error so a missing
record cannot be told apart from an unreadable one.
"""

import base64
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

import accounts
import authorization
import repository as repository_module


__all__ = [
    "WorkEntry",
    "WorkError",
    "WorkValidationError",
    "WorkCommit",
    "WorkSyncRequest",
    "WorkRestoreRequest",
    "validate_work_files",
    "decode_work_sync_request",
    "decode_work_restore_request",
    "current_work_head",
    "create_work_commit",
    "restore_work_commit",
    "MAX_WORK_ENTRY_COUNT",
    "MAX_WORK_FILE_BYTES",
    "MAX_WORK_TOTAL_BYTES",
    "MAX_SYNC_JSON_BODY_BYTES",
    "WORK_MANIFEST_PATH",
    "PICOPG_MANIFEST_PATH",
    "WORK_PREFIX",
    "PLAYGROUND_ALLOWED_PREFIXES",
    "SERVER_COMMIT_NAME",
    "SERVER_COMMIT_EMAIL",
    "REASON_CODES",
    # reason codes
    "REASON_WORK_NOT_ITERABLE",
    "REASON_INVALID_ENTRY",
    "REASON_PATH_NOT_STR",
    "REASON_PATH_NUL",
    "REASON_PATH_CONTROL_CHARACTER",
    "REASON_PATH_BACKSLASH",
    "REASON_PATH_INVALID_UNICODE",
    "REASON_PATH_ABSOLUTE",
    "REASON_PATH_EMPTY_SEGMENT",
    "REASON_PATH_DOT_SEGMENT",
    "REASON_PATH_NOT_ALLOWED",
    "REASON_ENTRY_KIND_NOT_FILE",
    "REASON_ENTRY_DATA_NOT_BYTES",
    "REASON_DUPLICATE_PATH",
    "REASON_TOO_MANY_ENTRIES",
    "REASON_FILE_TOO_LARGE",
    "REASON_WORK_TOO_LARGE",
    "REASON_WORK_NOT_NORMALIZED",
    "REASON_PLAYGROUND_TREE_INVALID",
    "REASON_PLAYGROUND_MISSING_MANIFEST",
    "REASON_PATH_OVERLAP",
    "REASON_WORK_MANIFEST_INVALID",
    "REASON_WORK_PLAYGROUND_MISMATCH",
    "REASON_INVALID_PAYLOAD",
    "REASON_INVALID_FILES",
    "REASON_INVALID_BASE64",
    "REASON_SOURCE_TREE_INVALID",
]


# --- Named server limits ---------------------------------------------------
#
# The submitted Work snapshot is bounded before any Git object is written.
MAX_WORK_ENTRY_COUNT = 1024
MAX_WORK_FILE_BYTES = 1024 * 1024
MAX_WORK_TOTAL_BYTES = 8 * 1024 * 1024

# A sync body carries base64 (a 4/3 expansion of the decoded bytes) plus JSON
# structural overhead and the unencoded paths.  Twice the decoded total is a
# safe, still bounded ceiling that rejects an oversized body before parsing.
MAX_SYNC_JSON_BODY_BYTES = 2 * MAX_WORK_TOTAL_BYTES

# --- Server-owned tree layout ----------------------------------------------
WORK_PREFIX = "work/"
WORK_MANIFEST_PATH = ".picopg/work.json"
PICOPG_MANIFEST_PATH = "picopg.json"
PLAYGROUND_ALLOWED_PREFIXES = ("course/", "starter/")

# The commit identity is a fixed Server identity; it is never derived from a
# request body.
SERVER_COMMIT_NAME = "PicoPG Server"
SERVER_COMMIT_EMAIL = "server@picopg.invalid"
SERVER_COMMIT_MESSAGE = "work"

# --- Stable reason codes ---------------------------------------------------
REASON_WORK_NOT_ITERABLE = "work_not_iterable"
REASON_INVALID_ENTRY = "invalid_entry"
REASON_PATH_NOT_STR = "path_not_str"
REASON_PATH_NUL = "path_nul"
REASON_PATH_CONTROL_CHARACTER = "path_control_character"
REASON_PATH_BACKSLASH = "path_backslash"
REASON_PATH_INVALID_UNICODE = "path_invalid_unicode"
REASON_PATH_ABSOLUTE = "path_absolute"
REASON_PATH_EMPTY_SEGMENT = "path_empty_segment"
REASON_PATH_DOT_SEGMENT = "path_dot_segment"
REASON_PATH_NOT_ALLOWED = "path_not_allowed"
REASON_ENTRY_KIND_NOT_FILE = "entry_kind_not_file"
REASON_ENTRY_DATA_NOT_BYTES = "entry_data_not_bytes"
REASON_DUPLICATE_PATH = "duplicate_path"
REASON_TOO_MANY_ENTRIES = "too_many_entries"
REASON_FILE_TOO_LARGE = "file_too_large"
REASON_WORK_TOO_LARGE = "work_too_large"
REASON_WORK_NOT_NORMALIZED = "work_not_normalized"
REASON_PLAYGROUND_TREE_INVALID = "playground_tree_invalid"
REASON_PLAYGROUND_MISSING_MANIFEST = "playground_missing_manifest"
REASON_PATH_OVERLAP = "path_overlap"
REASON_WORK_MANIFEST_INVALID = "work_manifest_invalid"
REASON_WORK_PLAYGROUND_MISMATCH = "work_playground_mismatch"
REASON_INVALID_PAYLOAD = "invalid_payload"
REASON_INVALID_FILES = "invalid_files"
REASON_INVALID_BASE64 = "invalid_base64"
REASON_SOURCE_TREE_INVALID = "source_tree_invalid"

REASON_CODES = frozenset(
    {
        REASON_WORK_NOT_ITERABLE,
        REASON_INVALID_ENTRY,
        REASON_PATH_NOT_STR,
        REASON_PATH_NUL,
        REASON_PATH_CONTROL_CHARACTER,
        REASON_PATH_BACKSLASH,
        REASON_PATH_INVALID_UNICODE,
        REASON_PATH_ABSOLUTE,
        REASON_PATH_EMPTY_SEGMENT,
        REASON_PATH_DOT_SEGMENT,
        REASON_PATH_NOT_ALLOWED,
        REASON_ENTRY_KIND_NOT_FILE,
        REASON_ENTRY_DATA_NOT_BYTES,
        REASON_DUPLICATE_PATH,
        REASON_TOO_MANY_ENTRIES,
        REASON_FILE_TOO_LARGE,
        REASON_WORK_TOO_LARGE,
        REASON_WORK_NOT_NORMALIZED,
        REASON_PLAYGROUND_TREE_INVALID,
        REASON_PLAYGROUND_MISSING_MANIFEST,
        REASON_PATH_OVERLAP,
        REASON_WORK_MANIFEST_INVALID,
        REASON_WORK_PLAYGROUND_MISMATCH,
        REASON_INVALID_PAYLOAD,
        REASON_INVALID_FILES,
        REASON_INVALID_BASE64,
        REASON_SOURCE_TREE_INVALID,
    }
)


class WorkError(Exception):
    """Raised when a Work commit cannot be created.

    ``reason`` is a stable code from :data:`REASON_CODES`.  The message is the
    reason code itself, so no path, content, or Git object id leaks through it.
    """

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class WorkValidationError(WorkError):
    """A submitted Work snapshot or assembled tree violates the contract."""


@dataclass(frozen=True)
class WorkEntry:
    """A small immutable already-decoded submitted Work file."""

    path: str
    data: bytes
    kind: str = "file"


@dataclass(frozen=True)
class WorkCommit:
    """Result of creating or appending one Server-owned Work commit.

    ``base_commit_id`` is the Work HEAD the caller observed, or ``None`` for
    the first commit.  The commit's actual parent is that base, or the
    Playground commit for the first commit.
    """

    playground_id: str
    student_id: str
    commit_id: str
    playground_commit_id: str
    base_commit_id: str | None


@dataclass(frozen=True)
class WorkSyncRequest:
    """One decoded, fully validated Work sync request.

    ``files`` is the immutable normalized mapping returned by
    :func:`validate_work_files`, so nothing downstream re-decodes base64 or
    trusts an unvalidated path.
    """

    playground_commit_id: str
    base_commit_id: str | None
    files: MappingProxyType


@dataclass(frozen=True)
class WorkRestoreRequest:
    """One decoded Work restore request.

    ``base_commit_id`` is the Work HEAD the caller observed and
    ``source_commit_id`` is the historical Work commit whose tree is restored.
    """

    base_commit_id: str
    source_commit_id: str


def _normalize_work_path(path):
    """Return the one normalized ``work/**`` path, or raise a reason."""

    if type(path) is not str:
        raise WorkValidationError(REASON_PATH_NOT_STR)
    if "\x00" in path:
        raise WorkValidationError(REASON_PATH_NUL)
    # Any other C0 control character or DEL is invalid in a Git tree path.
    # Reject it here so the domain validator owns the complete reason-code
    # contract instead of letting ``repository._is_safe_tree_path`` raise a
    # later ``RepositoryError``.
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in path):
        raise WorkValidationError(REASON_PATH_CONTROL_CHARACTER)
    if "\\" in path:
        raise WorkValidationError(REASON_PATH_BACKSLASH)
    # A lone surrogate is not a valid Unicode scalar value.  It cannot be
    # encoded as UTF-8, so it must be rejected here rather than reaching a
    # Git object write (``.encode("utf-8")``) and crashing.
    if any("\ud800" <= character <= "\udfff" for character in path):
        raise WorkValidationError(REASON_PATH_INVALID_UNICODE)

    normalized = unicodedata.normalize("NFC", path)

    if normalized.startswith("/"):
        raise WorkValidationError(REASON_PATH_ABSOLUTE)
    segments = normalized.split("/")
    if any(segment == "" for segment in segments):
        raise WorkValidationError(REASON_PATH_EMPTY_SEGMENT)
    if any(segment in (".", "..") for segment in segments):
        raise WorkValidationError(REASON_PATH_DOT_SEGMENT)

    # Only ``work/<something>`` is a client-submitted Work path.  This single
    # check rejects ``course/**``, ``starter/**``, ``picopg.json`` and
    # ``.picopg/**`` because none of them begin with the Work prefix.
    if (
        not normalized.startswith(WORK_PREFIX)
        or len(normalized) == len(WORK_PREFIX)
    ):
        raise WorkValidationError(REASON_PATH_NOT_ALLOWED)
    return normalized


_NOT_ITERABLE = object()


def _iter_entries(entries):
    try:
        return iter(entries)
    except TypeError:
        return _NOT_ITERABLE


def validate_work_files(entries):
    """Validate a decoded Work snapshot and return a normalized mapping.

    Only paths under ``work/`` are accepted, so a client can never contribute
    ``.picopg/**``, ``course/**``, ``starter/**`` or ``picopg.json``.  Entry
    count, per-file bytes, and total bytes are bounded by the named
    constants.  Nothing is returned unless the whole snapshot passes.
    """

    iterator = _iter_entries(entries)
    if iterator is _NOT_ITERABLE:
        raise WorkValidationError(REASON_WORK_NOT_ITERABLE)

    normalized = {}
    total_bytes = 0
    entry_count = 0

    for entry in iterator:
        # Exact type only: a WorkEntry subclass could lie about its fields.
        if type(entry) is not WorkEntry:
            raise WorkValidationError(REASON_INVALID_ENTRY)

        entry_count += 1
        if entry_count > MAX_WORK_ENTRY_COUNT:
            raise WorkValidationError(REASON_TOO_MANY_ENTRIES)

        path = entry.path
        kind = entry.kind
        data = entry.data

        normalized_path = _normalize_work_path(path)

        if type(kind) is not str or kind != "file":
            raise WorkValidationError(REASON_ENTRY_KIND_NOT_FILE)
        if type(data) is not bytes:
            raise WorkValidationError(REASON_ENTRY_DATA_NOT_BYTES)

        size = len(data)
        if size > MAX_WORK_FILE_BYTES:
            raise WorkValidationError(REASON_FILE_TOO_LARGE)
        total_bytes += size
        if total_bytes > MAX_WORK_TOTAL_BYTES:
            raise WorkValidationError(REASON_WORK_TOO_LARGE)

        if normalized_path in normalized:
            raise WorkValidationError(REASON_DUPLICATE_PATH)
        normalized[normalized_path] = data

    # Sorted for a deterministic view that does not depend on input order.
    return MappingProxyType(dict(sorted(normalized.items())))


def _normalized_work_snapshot(files):
    """Re-check that ``files`` is the exact validated Work mapping."""

    if type(files) is not MappingProxyType:
        raise WorkValidationError(REASON_WORK_NOT_NORMALIZED)
    return validate_work_files(
        [WorkEntry(path, data) for path, data in files.items()]
    )


def decode_work_sync_request(payload):
    """Decode and fully validate one Work sync JSON body.

    ``payload`` must be the parsed JSON object with ``playgroundCommitId``, a
    nullable ``baseCommitId`` and ``files``.  ``files`` is a list of
    ``{"path": <str>, "data": <base64 str>}`` entries that together are the
    complete ``work/**`` snapshot.  Base64 is decoded with strict validation
    and the whole snapshot is checked by :func:`validate_work_files` before
    anything is returned, so a malformed entry can never be applied
    partially.  No Git object is read or written here.
    """

    if type(payload) is not dict:
        raise WorkValidationError(REASON_INVALID_PAYLOAD)

    try:
        playground_commit_id = payload["playgroundCommitId"]
        base_commit_id = payload["baseCommitId"]
        raw_files = payload["files"]
    except KeyError:
        raise WorkValidationError(REASON_INVALID_PAYLOAD)

    if type(playground_commit_id) is not str or not playground_commit_id:
        raise WorkValidationError(REASON_INVALID_PAYLOAD)
    if base_commit_id is not None and (
        type(base_commit_id) is not str or not base_commit_id
    ):
        raise WorkValidationError(REASON_INVALID_PAYLOAD)

    if type(raw_files) is not list:
        raise WorkValidationError(REASON_INVALID_FILES)
    # Reject an over-long list before decoding any entry so a huge snapshot
    # cannot make the Server allocate per-entry buffers first.
    if len(raw_files) > MAX_WORK_ENTRY_COUNT:
        raise WorkValidationError(REASON_TOO_MANY_ENTRIES)

    entries = []
    for entry in raw_files:
        if type(entry) is not dict:
            raise WorkValidationError(REASON_INVALID_ENTRY)
        path = entry.get("path")
        encoded = entry.get("data")
        if type(encoded) is not str:
            raise WorkValidationError(REASON_ENTRY_DATA_NOT_BYTES)
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise WorkValidationError(REASON_INVALID_BASE64)
        entries.append(WorkEntry(path, data))

    return WorkSyncRequest(
        playground_commit_id=playground_commit_id,
        base_commit_id=base_commit_id,
        files=validate_work_files(entries),
    )


def decode_work_restore_request(payload):
    """Decode and validate one Work restore JSON body.

    ``payload`` must carry a non-empty string ``baseCommitId`` (the Work HEAD
    the caller observed) and a non-empty string ``sourceCommitId`` (the
    historical Work commit to restore).  The object ids are only checked for
    shape here; history and authorization are established when the request is
    applied.  No Git object is read or written here.
    """

    if type(payload) is not dict:
        raise WorkValidationError(REASON_INVALID_PAYLOAD)

    try:
        base_commit_id = payload["baseCommitId"]
        source_commit_id = payload["sourceCommitId"]
    except KeyError:
        raise WorkValidationError(REASON_INVALID_PAYLOAD)

    if type(base_commit_id) is not str or not base_commit_id:
        raise WorkValidationError(REASON_INVALID_PAYLOAD)
    if type(source_commit_id) is not str or not source_commit_id:
        raise WorkValidationError(REASON_INVALID_PAYLOAD)

    return WorkRestoreRequest(
        base_commit_id=base_commit_id,
        source_commit_id=source_commit_id,
    )


def _playground_path_is_allowed(path):
    if path == PICOPG_MANIFEST_PATH:
        return True
    for prefix in PLAYGROUND_ALLOWED_PREFIXES:
        if path.startswith(prefix) and len(path) > len(prefix):
            return True
    return False


def _read_playground_tree(repository, playground_commit_id):
    """Read the exact Playground tree into a ``path -> bytes`` mapping.

    Only the fixed Playground layout is accepted; a crafted commit carrying a
    Work path or a non-file entry is rejected instead of being copied into the
    new Work tree.  The tree must contain ``picopg.json``: a Playground commit
    without the manifest is not a valid Playground, and its blobs are never
    read.
    """

    entries = repository_module.list_commit_tree(
        repository, playground_commit_id
    )
    planned = []
    has_manifest = False
    for entry in entries:
        if entry.kind != "blob" or entry.mode not in ("100644", "100755"):
            raise WorkValidationError(REASON_PLAYGROUND_TREE_INVALID)
        if entry.size is None:
            raise WorkValidationError(REASON_PLAYGROUND_TREE_INVALID)
        if not _playground_path_is_allowed(entry.path):
            raise WorkValidationError(REASON_PLAYGROUND_TREE_INVALID)
        if entry.path == PICOPG_MANIFEST_PATH:
            has_manifest = True
        planned.append(entry)

    if not has_manifest:
        raise WorkValidationError(REASON_PLAYGROUND_MISSING_MANIFEST)

    blobs = repository_module.read_blobs(
        repository, [entry.object_id for entry in planned]
    )

    files = {}
    for entry in planned:
        data = blobs.get(entry.object_id)
        if data is None or len(data) != entry.size:
            raise WorkValidationError(REASON_PLAYGROUND_TREE_INVALID)
        if entry.path in files:
            raise WorkValidationError(REASON_PLAYGROUND_TREE_INVALID)
        files[entry.path] = data
    return files


def _work_manifest(playground_commit_id):
    """Return the Server-owned ``.picopg/work.json`` bytes."""

    return json.dumps(
        {"playgroundCommitId": playground_commit_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _read_base_work_playground_commit_id(repository, base_commit_id):
    """Return the Playground commit pinned by a base Work commit.

    A base commit that is missing, has no ``.picopg/work.json``, or records
    anything other than a string ``playgroundCommitId`` fails closed with a
    stable :class:`WorkValidationError` instead of crashing or silently
    accepting the base.
    """

    resolved = repository_module.resolve_commit(repository, base_commit_id)
    if resolved is None:
        raise WorkValidationError(REASON_WORK_MANIFEST_INVALID)

    entries = repository_module.list_commit_tree(repository, resolved)
    manifests = [
        entry for entry in entries if entry.path == WORK_MANIFEST_PATH
    ]
    if len(manifests) != 1:
        raise WorkValidationError(REASON_WORK_MANIFEST_INVALID)
    manifest_entry = manifests[0]
    if manifest_entry.kind != "blob" or manifest_entry.size is None:
        raise WorkValidationError(REASON_WORK_MANIFEST_INVALID)

    blobs = repository_module.read_blobs(
        repository, [manifest_entry.object_id]
    )
    data = blobs.get(manifest_entry.object_id)
    if data is None or len(data) != manifest_entry.size:
        raise WorkValidationError(REASON_WORK_MANIFEST_INVALID)
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WorkValidationError(REASON_WORK_MANIFEST_INVALID)
    if type(document) is not dict:
        raise WorkValidationError(REASON_WORK_MANIFEST_INVALID)
    pinned = document.get("playgroundCommitId")
    if type(pinned) is not str:
        raise WorkValidationError(REASON_WORK_MANIFEST_INVALID)
    return pinned


def _server_timestamp():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def current_work_head(repository, playground_id, student_id):
    """Return the exact Work HEAD for one Student, or ``None``.

    Callers use this only after ownership is established, so a conflict
    response can name the caller's own canonical Work HEAD and never another
    Student's.
    """

    return repository_module.resolve_work_head(
        repository, playground_id, student_id
    )


def create_work_commit(
    connection,
    actor,
    repository,
    playground_id,
    playground_commit_id,
    files,
    *,
    base_commit_id=None,
    now=None,
):
    """Create or append one Student's Server-owned Work commit.

    ``files`` must be the normalized mapping returned by
    :func:`validate_work_files`.  ``base_commit_id`` is the Work HEAD the
    caller observed; it is ``None`` for the first commit.  The Server derives
    the Work ref from the authenticated Student and the Playground, reads the
    exact Playground commit, and writes the full tree.  Every denial raises
    :class:`authorization.ContentAccessDeniedError` before any content read.
    """

    # A Work belongs to exactly one Student and the owner is the authenticated
    # actor.  A caller can never name another Student's Work ref.
    if not isinstance(actor, accounts.User) or actor.role != "student":
        raise authorization.ContentAccessDeniedError(
            "content is not readable"
        )
    student_id = actor.id

    authorization.authorize_content_read(
        connection,
        actor,
        repository,
        playground_id,
        playground_commit_id,
        now=now,
    )

    # A later Work commit may only append to the exact Work history the caller
    # observed, and it may never change the Playground commit that history was
    # pinned to.  The pinned version is Server-owned: it is read back from the
    # base commit's ``.picopg/work.json`` and compared before any new state is
    # written.  A missing or malformed base manifest fails closed.
    if base_commit_id is not None:
        head = repository_module.resolve_work_head(
            repository, playground_id, student_id
        )
        if head is None or head != base_commit_id:
            raise repository_module.WorkHeadChangedError()
        base_playground_commit_id = _read_base_work_playground_commit_id(
            repository, base_commit_id
        )
        if base_playground_commit_id != playground_commit_id:
            raise WorkValidationError(REASON_WORK_PLAYGROUND_MISMATCH)

    work_files = _normalized_work_snapshot(files)
    playground_files = _read_playground_tree(
        repository, playground_commit_id
    )

    full_tree = dict(playground_files)
    for path, data in work_files.items():
        if path in full_tree:
            raise WorkValidationError(REASON_PATH_OVERLAP)
        full_tree[path] = data
    full_tree[WORK_MANIFEST_PATH] = _work_manifest(playground_commit_id)

    metadata = repository_module.CommitMetadata(
        name=SERVER_COMMIT_NAME,
        email=SERVER_COMMIT_EMAIL,
        timestamp=_server_timestamp(),
        message=SERVER_COMMIT_MESSAGE,
    )

    commit_id = repository_module.write_work_commit(
        repository,
        playground_id,
        student_id,
        playground_commit_id,
        base_commit_id,
        MappingProxyType(dict(full_tree)),
        metadata,
    )

    return WorkCommit(
        playground_id=playground_id,
        student_id=student_id,
        commit_id=commit_id,
        playground_commit_id=playground_commit_id,
        base_commit_id=base_commit_id,
    )


def _read_work_tree(repository, commit_id):
    """Read one Work commit's complete tree into a ``path -> bytes`` mapping.

    Only plain blobs with the canonical Server mode are accepted.  A crafted
    commit carrying a submodule, a non-file entry, or an unexpected mode is
    rejected instead of being silently rewritten into a different tree.
    """

    entries = repository_module.list_commit_tree(repository, commit_id)
    planned = []
    for entry in entries:
        if entry.kind != "blob" or entry.mode != "100644":
            raise WorkValidationError(REASON_SOURCE_TREE_INVALID)
        if entry.size is None:
            raise WorkValidationError(REASON_SOURCE_TREE_INVALID)
        planned.append(entry)

    blobs = repository_module.read_blobs(
        repository, [entry.object_id for entry in planned]
    )
    files = {}
    for entry in planned:
        data = blobs.get(entry.object_id)
        if data is None or len(data) != entry.size:
            raise WorkValidationError(REASON_SOURCE_TREE_INVALID)
        if entry.path in files:
            raise WorkValidationError(REASON_SOURCE_TREE_INVALID)
        files[entry.path] = data
    return MappingProxyType(dict(files))


def restore_work_commit(
    connection,
    actor,
    repository,
    playground_id,
    base_commit_id,
    source_commit_id,
    *,
    now=None,
):
    """Append a new Work commit whose tree is an older Work commit's tree.

    Restoring is append-only: the new commit has the current Work HEAD as its
    sole parent and the ref is compare-and-swapped to it, so no history is
    moved, reset, or merged.  ``base_commit_id`` must equal the current HEAD;
    ``source_commit_id`` must be a commit already in that Work's history with
    the same Server-pinned Playground commit.  A foreign or non-ancestor
    source raises :class:`authorization.ContentAccessDeniedError`; a stale base
    raises :class:`repository_module.WorkHeadChangedError` and never moves the
    ref.
    """

    # A Work belongs to exactly one Student and only that Student may restore
    # it.  The Work ref is derived from the actor, so another Student's Work
    # can never be named here.
    if not isinstance(actor, accounts.User) or actor.role != "student":
        raise authorization.ContentAccessDeniedError(
            "content is not readable"
        )
    student_id = actor.id

    # Restore never rewinds the ref: it only appends to the exact HEAD the
    # caller observed, so a missing or moved HEAD is a conflict.
    head = repository_module.resolve_work_head(
        repository, playground_id, student_id
    )
    if head is None or head != base_commit_id:
        raise repository_module.WorkHeadChangedError()

    # The source must already belong to this Student's Work history; a foreign
    # or non-ancestor commit is denied through the one authorization error
    # before any of its content is read.
    authorization.authorize_content_read(
        connection,
        actor,
        repository,
        playground_id,
        source_commit_id,
        work_student_id=student_id,
        now=now,
    )

    # The pinned Playground commit is Server-owned.  A source with a missing,
    # malformed, or different pin fails closed before the ref can move.
    source_playground_commit_id = _read_base_work_playground_commit_id(
        repository, source_commit_id
    )
    head_playground_commit_id = _read_base_work_playground_commit_id(
        repository, head
    )
    if source_playground_commit_id != head_playground_commit_id:
        raise WorkValidationError(REASON_WORK_PLAYGROUND_MISMATCH)

    # Rebuild the exact source tree and append it as a normal Work commit.
    files = _read_work_tree(repository, source_commit_id)

    metadata = repository_module.CommitMetadata(
        name=SERVER_COMMIT_NAME,
        email=SERVER_COMMIT_EMAIL,
        timestamp=_server_timestamp(),
        message=SERVER_COMMIT_MESSAGE,
    )

    commit_id = repository_module.write_work_commit(
        repository,
        playground_id,
        student_id,
        source_playground_commit_id,
        base_commit_id,
        files,
        metadata,
    )

    return WorkCommit(
        playground_id=playground_id,
        student_id=student_id,
        commit_id=commit_id,
        playground_commit_id=source_playground_commit_id,
        base_commit_id=base_commit_id,
    )
