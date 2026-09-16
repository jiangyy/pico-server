"""Deterministic manifests for authorized immutable Playground commits.

A manifest is derived from one exact Playground commit that the caller is
authorized to read.  Authorization runs first against the exact
``(playgroundId, commitId)`` pair; only then is the commit's tree listed and
its blob bytes hashed.  The commit is never substituted with the current branch
HEAD, no checkout or temporary copy is created, and nothing is cached.

The manifest is a pure value: the same commit always produces the same
path-ordered file list and the same deterministic JSON bytes.
"""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

import accounts
import authorization
import bundles
import repository as repository_module


__all__ = [
    "CommitFile",
    "CommitManifest",
    "ManifestError",
    "ManifestFile",
    "current_playground_commit",
    "generate_manifest",
    "manifest_to_mapping",
    "normalize_content_path",
    "read_commit_file",
    "serialize_manifest",
]


# The manifest mirrors the publishing bundle contract: same allowlist, same
# entry count, and the same per-file and total byte limits.
MAX_ENTRY_COUNT = bundles.MAX_ENTRY_COUNT
MAX_FILE_BYTES = bundles.MAX_FILE_BYTES
MAX_TOTAL_BYTES = bundles.MAX_TOTAL_BYTES
MANIFEST_PATH = bundles.MANIFEST_PATH
ALLOWED_PREFIXES = bundles.ALLOWED_PREFIXES

# Publishing only ever writes ordinary non-executable files.  Regular
# executable blobs are still regular files; symlinks (120000) and submodule
# gitlinks (160000) are not files and must never appear in a manifest.
ALLOWED_FILE_MODES = frozenset({"100644", "100755"})

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")

# Stable reason codes.  The message is the reason itself, so no path, blob
# content, or Git object id can leak through an error string.
REASON_TOO_MANY_ENTRIES = "too_many_entries"
REASON_FILE_TOO_LARGE = "file_too_large"
REASON_BUNDLE_TOO_LARGE = "bundle_too_large"
REASON_ENTRY_NOT_BLOB = "entry_not_blob"
REASON_ENTRY_MODE_NOT_FILE = "entry_mode_not_file"
REASON_PATH_NOT_ALLOWED = "path_not_allowed"
REASON_PATH_NOT_FOUND = "path_not_found"
REASON_PATH_CASE_COLLISION = "path_case_collision"
REASON_DUPLICATE_PATH = "duplicate_path"
REASON_MISSING_BLOB = "missing_blob"
REASON_BLOB_SIZE_MISMATCH = "blob_size_mismatch"

REASON_CODES = frozenset(
    {
        REASON_TOO_MANY_ENTRIES,
        REASON_FILE_TOO_LARGE,
        REASON_BUNDLE_TOO_LARGE,
        REASON_ENTRY_NOT_BLOB,
        REASON_ENTRY_MODE_NOT_FILE,
        REASON_PATH_NOT_ALLOWED,
        REASON_PATH_NOT_FOUND,
        REASON_PATH_CASE_COLLISION,
        REASON_DUPLICATE_PATH,
        REASON_MISSING_BLOB,
        REASON_BLOB_SIZE_MISMATCH,
    }
)


class ManifestError(Exception):
    """Raised when an authorized commit cannot produce a valid manifest.

    ``reason`` is a stable code from :data:`REASON_CODES`.  The message is the
    reason code itself, so a malformed tree never leaks a path or content.
    """

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


# Unauthenticated content requests are evaluated with an explicit anonymous
# actor.  The centralized authorization rules therefore still make the
# public/private decision instead of the HTTP layer duplicating policy: only a
# public, unarchived Playground commit is reachable without an account.
_ANONYMOUS_ACTOR = accounts.User(
    id="anonymous",
    username="anonymous",
    role="anonymous",
    created_at="",
)


@dataclass(frozen=True)
class ManifestFile:
    """One file in a commit manifest."""

    path: str
    size: int
    sha256: str
    blob_id: str


@dataclass(frozen=True)
class CommitFile:
    """One exact downloadable file from an authorized commit."""

    path: str
    data: bytes
    sha256: str


@dataclass(frozen=True)
class CommitManifest:
    """Deterministic manifest of one immutable Playground commit."""

    commit_id: str
    files: tuple


def _path_is_allowed(path):
    """Return whether ``path`` is one of the exact allowed manifest paths."""

    if type(path) is not str or not path:
        return False
    if "\\" in path or _CONTROL_CHARACTERS.search(path):
        return False
    if path.startswith("/"):
        return False
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return False
    if path == MANIFEST_PATH:
        return True
    for prefix in ALLOWED_PREFIXES:
        if path.startswith(prefix) and len(path) > len(prefix):
            break
    else:
        return False
    for segment in segments:
        if bundles.portable_segment_reason(segment) is not None:
            return False
    return True


def _build_manifest(repository, commit_id):
    entries = repository_module.list_commit_tree(repository, commit_id)
    if len(entries) > MAX_ENTRY_COUNT:
        raise ManifestError(REASON_TOO_MANY_ENTRIES)

    seen = set()
    folded = set()
    total_bytes = 0
    planned = []

    for entry in entries:
        if not _path_is_allowed(entry.path):
            raise ManifestError(REASON_PATH_NOT_ALLOWED)
        if entry.path in seen:
            raise ManifestError(REASON_DUPLICATE_PATH)
        folded_path = entry.path.casefold()
        if folded_path in folded:
            raise ManifestError(REASON_PATH_CASE_COLLISION)
        seen.add(entry.path)
        folded.add(folded_path)

        if entry.kind != "blob":
            raise ManifestError(REASON_ENTRY_NOT_BLOB)
        if entry.mode not in ALLOWED_FILE_MODES:
            raise ManifestError(REASON_ENTRY_MODE_NOT_FILE)
        if entry.size is None:
            raise ManifestError(REASON_ENTRY_NOT_BLOB)
        if entry.size > MAX_FILE_BYTES:
            raise ManifestError(REASON_FILE_TOO_LARGE)
        total_bytes += entry.size
        if total_bytes > MAX_TOTAL_BYTES:
            raise ManifestError(REASON_BUNDLE_TOO_LARGE)
        planned.append(entry)

    blobs = repository_module.read_blobs(
        repository, [entry.object_id for entry in planned]
    )

    files = []
    for entry in planned:
        data = blobs.get(entry.object_id)
        if data is None:
            raise ManifestError(REASON_MISSING_BLOB)
        if len(data) != entry.size:
            raise ManifestError(REASON_BLOB_SIZE_MISMATCH)
        files.append(
            ManifestFile(
                path=entry.path,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                blob_id=entry.object_id,
            )
        )

    files.sort(key=lambda manifest_file: manifest_file.path)
    return CommitManifest(commit_id=commit_id, files=tuple(files))


def generate_manifest(
    connection,
    actor,
    repository,
    playground_id,
    commit_id,
    *,
    now=None,
):
    """Return the deterministic manifest of one authorized Playground commit.

    Authorization of the exact ``(playground_id, commit_id)`` pair happens
    before any tree or blob content is read, so a missing or unauthorized
    target raises :class:`authorization.ContentAccessDeniedError` and no Git
    content access is attempted.  A reachable commit whose tree violates the
    manifest contract raises :class:`ManifestError`.
    """

    authorization.authorize_content_read(
        connection,
        _resolve_actor(actor),
        repository,
        playground_id,
        commit_id,
        now=now,
    )
    return _build_manifest(repository, commit_id)


def _resolve_actor(actor):
    """Return the actor to authorize, or the anonymous public-only actor."""

    return _ANONYMOUS_ACTOR if actor is None else actor


def normalize_content_path(path):
    """Return the canonical allowed path for a download request.

    The caller's already-decoded path is Unicode-normalized exactly as the
    publishing bundle rules do, then checked against the same allowlist.  A
    path outside that allowlist raises :class:`ManifestError` with
    :data:`REASON_PATH_NOT_ALLOWED`.
    """

    if type(path) is not str or not path:
        raise ManifestError(REASON_PATH_NOT_ALLOWED)
    normalized = unicodedata.normalize("NFC", path)
    if not _path_is_allowed(normalized):
        raise ManifestError(REASON_PATH_NOT_ALLOWED)
    return normalized


def current_playground_commit(repository, playground_id):
    """Return the current commit of an existing Playground, else ``None``.

    Only the namespaced Playground ref is used, so a caller can never supply a
    ref name.  A missing or unreadable ref yields ``None`` instead of an error.
    """

    try:
        ref = repository.playground_ref(playground_id)
    except repository_module.RepositoryError:
        return None
    try:
        return repository_module._resolve_head(repository, ref)
    except repository_module.RepositoryError:
        return None


def read_commit_file(
    connection,
    actor,
    repository,
    playground_id,
    commit_id,
    path,
    *,
    now=None,
):
    """Return one exact file from an authorized Playground commit.

    The exact ``(playground_id, commit_id)`` pair is authorized before any
    commit tree or blob is read.  The requested path is then resolved inside
    that commit's own tree; a caller-supplied blob id is never accepted.  A
    path that is not allowed or not present raises :class:`ManifestError`.
    """

    authorization.authorize_content_read(
        connection,
        _resolve_actor(actor),
        repository,
        playground_id,
        commit_id,
        now=now,
    )

    normalized_path = normalize_content_path(path)
    entries = repository_module.list_commit_tree(repository, commit_id)

    target = None
    for entry in entries:
        if entry.path == normalized_path:
            target = entry
            break
    if target is None:
        raise ManifestError(REASON_PATH_NOT_FOUND)

    if target.kind != "blob":
        raise ManifestError(REASON_ENTRY_NOT_BLOB)
    if target.mode not in ALLOWED_FILE_MODES:
        raise ManifestError(REASON_ENTRY_MODE_NOT_FILE)
    if target.size is None:
        raise ManifestError(REASON_ENTRY_NOT_BLOB)
    if target.size > MAX_FILE_BYTES:
        raise ManifestError(REASON_FILE_TOO_LARGE)

    blobs = repository_module.read_blobs(repository, [target.object_id])
    data = blobs.get(target.object_id)
    if data is None:
        raise ManifestError(REASON_MISSING_BLOB)
    if len(data) != target.size:
        raise ManifestError(REASON_BLOB_SIZE_MISMATCH)

    return CommitFile(
        path=target.path,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def manifest_to_mapping(manifest):
    """Return the canonical camelCase mapping for a manifest."""

    if type(manifest) is not CommitManifest:
        raise TypeError("manifest must be a CommitManifest")
    return {
        "commitId": manifest.commit_id,
        "files": [
            {
                "path": manifest_file.path,
                "size": manifest_file.size,
                "sha256": manifest_file.sha256,
                "blobId": manifest_file.blob_id,
            }
            for manifest_file in manifest.files
        ],
    }


def serialize_manifest(manifest):
    """Return deterministic UTF-8 JSON bytes for ``manifest``.

    Keys are sorted and separators are minimal, so serializing the same
    manifest always produces byte-identical output.
    """

    return json.dumps(
        manifest_to_mapping(manifest),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
