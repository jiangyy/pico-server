"""Read-only description and reads of the ``work/**`` files in a Work commit.

This module is the one narrow service that can describe and read the
client-owned ``work/**`` snapshot of an exact Work commit.  It never exposes
Git blob ids, filesystem paths, commit messages, author fields, Playground
source, ``.picopg/work.json``, or arbitrary Git objects: a manifest lists only
portable paths relative to ``work/`` with their size and SHA-256, and a file
read returns exact bytes for a path already present in that manifest.

Every call re-establishes the durable facts instead of trusting an HTTP
parameter or a database row:

1. authorization proves the actor may read this exact Student/Work pair;
2. the commit is reachable from that Student's exact Work ref;
3. its Server-owned metadata pins a commit published for this Playground;
4. its tree is the Work schema (Playground content plus one metadata file plus
   portable ``work/**`` files) and nothing else.

Authorization is deliberately not re-implemented here; this module calls the
central :func:`authorization.authorize_content_read` so there is exactly one
externally distinguishable denial.
"""

import hashlib
from dataclasses import dataclass

import authorization
import bundles
import repository as repository_module
import works


__all__ = [
    "WorkContentError",
    "WorkFile",
    "WorkFileContent",
    "WorkManifest",
    "get_work_manifest",
    "read_work_file",
    "REASON_WORK_TREE_INVALID",
    "REASON_WORK_METADATA_INVALID",
    "REASON_PLAYGROUND_COMMIT_INVALID",
    "REASON_TOO_MANY_ENTRIES",
    "REASON_FILE_TOO_LARGE",
    "REASON_WORK_TOO_LARGE",
    "REASON_PATH_NOT_FOUND",
    "REASON_PATH_NOT_ALLOWED",
]


WORK_PREFIX = works.WORK_PREFIX
WORK_MANIFEST_PATH = works.WORK_MANIFEST_PATH
PLAYGROUND_MANIFEST_PATH = works.PICOPG_MANIFEST_PATH

# Stable internal-integrity reason codes.  A caller maps these to its own
# bounded error response; they never contain a path, blob, or commit message.
REASON_WORK_TREE_INVALID = "work_tree_invalid"
REASON_WORK_METADATA_INVALID = "work_metadata_invalid"
REASON_PLAYGROUND_COMMIT_INVALID = "playground_commit_invalid"
REASON_TOO_MANY_ENTRIES = "too_many_entries"
REASON_FILE_TOO_LARGE = "file_too_large"
REASON_WORK_TOO_LARGE = "work_too_large"
REASON_PATH_NOT_FOUND = "path_not_found"
REASON_PATH_NOT_ALLOWED = "path_not_allowed"


class WorkContentError(Exception):
    """Raised when a Work commit's durable Git data is invalid or missing.

    This is an internal-integrity failure, not an authorization decision: the
    caller has already been authorized to read the Work.  ``reason`` is a
    stable code from this module's ``REASON_*`` constants.
    """

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class WorkFile:
    """One ``work/**`` file described by a manifest, without the prefix."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class WorkManifest:
    """The ordered ``work/**`` manifest of one validated Work commit."""

    playground_id: str
    work_commit_id: str
    playground_commit_id: str
    files: tuple


@dataclass(frozen=True)
class WorkFileContent:
    """One exact file read from a validated Work commit."""

    path: str
    data: bytes
    sha256: str


def get_work_manifest(
    connection,
    actor,
    repository,
    student_id,
    playground_id,
    work_commit_id,
    *,
    now=None,
):
    """Return the validated ``work/**`` manifest of one Work commit.

    ``student_id`` and ``playground_id`` must name the exact Work whose commit
    is requested; the Work ref is derived from them and never from a raw ref
    supplied by a caller.
    """

    _authorize(
        connection,
        actor,
        repository,
        student_id,
        playground_id,
        work_commit_id,
        now,
    )
    manifest, _ = _validated_work_files(
        repository, playground_id, work_commit_id
    )
    return manifest


def read_work_file(
    connection,
    actor,
    repository,
    student_id,
    playground_id,
    work_commit_id,
    path,
    *,
    now=None,
):
    """Return one exact ``work/**`` file's bytes from a Work commit.

    ``path`` is a manifest path without the ``work/`` prefix.  Only a path
    already present in the validated manifest is readable; a directory is
    never readable as a file and an empty file returns empty bytes.
    """

    _authorize(
        connection,
        actor,
        repository,
        student_id,
        playground_id,
        work_commit_id,
        now,
    )
    normalized = _normalize_work_file_path(path)

    # Building the manifest is what proves the tree is a valid Work tree and
    # that this exact path is one of its client files.
    _, files = _validated_work_files(
        repository, playground_id, work_commit_id
    )
    data = files.get(normalized)
    if data is None:
        raise WorkContentError(REASON_PATH_NOT_FOUND)
    return WorkFileContent(
        path=normalized,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _authorize(
    connection,
    actor,
    repository,
    student_id,
    playground_id,
    work_commit_id,
    now,
):
    authorization.authorize_content_read(
        connection,
        actor,
        repository,
        playground_id,
        work_commit_id,
        work_student_id=student_id,
        now=now,
    )


def _normalize_work_file_path(path):
    """Return the exact portable relative path, or raise a bounded reason."""

    if type(path) is not str or not path:
        raise WorkContentError(REASON_PATH_NOT_ALLOWED)
    if "\\" in path or path.startswith("/"):
        raise WorkContentError(REASON_PATH_NOT_ALLOWED)
    segments = path.split("/")
    if any(segment == "" for segment in segments):
        raise WorkContentError(REASON_PATH_NOT_ALLOWED)
    if any(segment in (".", "..") for segment in segments):
        raise WorkContentError(REASON_PATH_NOT_ALLOWED)
    # ``path`` is relative to ``work/``; any Playground, manifest, or
    # Server-owned path is not a client Work file.
    if path in (PLAYGROUND_MANIFEST_PATH, WORK_MANIFEST_PATH) or path.startswith(
        (WORK_PREFIX, "course/", "starter/")
    ):
        raise WorkContentError(REASON_PATH_NOT_ALLOWED)
    for segment in segments:
        if bundles.portable_segment_reason(segment) is not None:
            raise WorkContentError(REASON_PATH_NOT_ALLOWED)
    return path


def _tree_path_is_allowed(path):
    if path in (PLAYGROUND_MANIFEST_PATH, WORK_MANIFEST_PATH):
        return True
    for prefix in ("course/", "starter/", WORK_PREFIX):
        if path.startswith(prefix) and len(path) > len(prefix):
            return True
    return False


def _tree_path_is_portable(path):
    return all(
        bundles.portable_segment_reason(segment) is None
        for segment in path.split("/")
    )


def _validate_tree_entry(entry):
    if entry.kind != "blob" or entry.mode != "100644":
        raise WorkContentError(REASON_WORK_TREE_INVALID)
    if entry.size is None:
        raise WorkContentError(REASON_WORK_TREE_INVALID)
    if not _tree_path_is_allowed(entry.path):
        raise WorkContentError(REASON_WORK_TREE_INVALID)
    if not _tree_path_is_portable(entry.path):
        raise WorkContentError(REASON_WORK_TREE_INVALID)


def _work_metadata_pin(repository, work_commit_id):
    try:
        return works._read_base_work_playground_commit_id(
            repository, work_commit_id
        )
    except works.WorkError as error:
        raise WorkContentError(REASON_WORK_METADATA_INVALID) from error


def _validated_work_files(repository, playground_id, work_commit_id):
    """Return ``(manifest, {relative path: bytes})`` for one Work commit."""

    pinned = _work_metadata_pin(repository, work_commit_id)
    if not authorization.playground_commit_is_published(
        repository, playground_id, pinned
    ):
        raise WorkContentError(REASON_PLAYGROUND_COMMIT_INVALID)

    entries = repository_module.list_commit_tree(repository, work_commit_id)
    metadata = [
        entry for entry in entries if entry.path == WORK_MANIFEST_PATH
    ]
    if len(metadata) != 1:
        raise WorkContentError(REASON_WORK_METADATA_INVALID)

    selected = []
    total_bytes = 0
    for entry in entries:
        _validate_tree_entry(entry)
        if entry.path == WORK_MANIFEST_PATH:
            continue
        if entry.path.startswith(WORK_PREFIX) and len(entry.path) > len(
            WORK_PREFIX
        ):
            if entry.size > works.MAX_WORK_FILE_BYTES:
                raise WorkContentError(REASON_FILE_TOO_LARGE)
            total_bytes += entry.size
            if total_bytes > works.MAX_WORK_TOTAL_BYTES:
                raise WorkContentError(REASON_WORK_TOO_LARGE)
            if len(selected) >= works.MAX_WORK_ENTRY_COUNT:
                raise WorkContentError(REASON_TOO_MANY_ENTRIES)
            selected.append(entry)

    blobs = repository_module.read_blobs(
        repository, [entry.object_id for entry in selected]
    )
    files = {}
    for entry in selected:
        data = blobs.get(entry.object_id)
        if data is None or len(data) != entry.size:
            raise WorkContentError(REASON_WORK_TREE_INVALID)
        relative = entry.path[len(WORK_PREFIX):]
        if relative in files:
            raise WorkContentError(REASON_WORK_TREE_INVALID)
        files[relative] = data

    ordered = sorted(files.items(), key=lambda item: item[0].encode("utf-8"))
    manifest = WorkManifest(
        playground_id=playground_id,
        work_commit_id=work_commit_id,
        playground_commit_id=pinned,
        files=tuple(
            WorkFile(
                path=path,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
            for path, data in ordered
        ),
    )
    return manifest, files
