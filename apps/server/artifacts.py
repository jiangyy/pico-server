"""Immutable runtime artifact indexing and verified reads.

An injected artifacts directory stores immutable runtime/EXE files as::

    <root>/<version>/<filename>

``version`` is an opaque identifier, not a number to compare or a timestamp,
and each version holds exactly one ordinary file.  An index entry records only
the opaque version, the filename, the byte size and the SHA-256 of the exact
bytes.

An artifact is data.  It is read into memory only after its size and hash have
been checked against the index entry the caller already trusts; it is never
executed, imported, unpacked, or otherwise interpreted.  The index is a
generated cache, so it never grants access to anything: a caller must present
the exact entry it wants to serve.
"""

import hashlib
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path


__all__ = [
    "ArtifactEntry",
    "ArtifactError",
    "REASON_CHECKSUM_MISMATCH",
    "REASON_DUPLICATE_VERSION",
    "REASON_INVALID_INDEX",
    "REASON_INVALID_VERSION",
    "REASON_MISSING_ARTIFACT",
    "REASON_MUTABLE_REPLACEMENT",
    "REASON_PATH_ESCAPE",
    "REASON_UNEXPECTED_ENTRY",
    "entries_by_version",
    "index_artifacts",
    "index_to_mapping",
    "mapping_to_index",
    "serve_artifact",
    "verify_artifact",
]


# Stable reason codes.  The message is the reason itself so a path or content
# can never leak through an exception string.
REASON_CHECKSUM_MISMATCH = "checksum_mismatch"
REASON_DUPLICATE_VERSION = "duplicate_version"
REASON_INVALID_INDEX = "invalid_index"
REASON_INVALID_VERSION = "invalid_version"
REASON_MISSING_ARTIFACT = "missing_artifact"
REASON_MUTABLE_REPLACEMENT = "mutable_replacement"
REASON_PATH_ESCAPE = "path_escape"
REASON_UNEXPECTED_ENTRY = "unexpected_entry"

REASON_CODES = frozenset(
    {
        REASON_CHECKSUM_MISMATCH,
        REASON_DUPLICATE_VERSION,
        REASON_INVALID_INDEX,
        REASON_INVALID_VERSION,
        REASON_MISSING_ARTIFACT,
        REASON_MUTABLE_REPLACEMENT,
        REASON_PATH_ESCAPE,
        REASON_UNEXPECTED_ENTRY,
    }
)


class ArtifactError(Exception):
    """Raised when an artifact directory or entry violates the contract."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ArtifactEntry:
    """One immutable artifact: opaque version, filename, size and SHA-256."""

    version: str
    filename: str
    size: int
    sha256: str

    def to_mapping(self):
        return {
            "version": self.version,
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
        }


def _validate_segment(value):
    """Return whether ``value`` is one safe, canonical path segment."""

    if type(value) is not str or not value:
        return False
    if value in (".", ".."):
        return False
    if "/" in value or "\\" in value:
        return False
    if any(character < " " or character == "\x7f" for character in value):
        return False
    # A non-normalized name could collide with a normalized sibling on another
    # filesystem, so only the exact NFC form is accepted.
    if unicodedata.normalize("NFC", value) != value:
        return False
    return True


def _real_directory(root):
    """Return the resolved ``root`` or raise when it is not a real directory."""

    root = Path(root)
    if root.is_symlink():
        raise ArtifactError(REASON_PATH_ESCAPE)
    if not root.exists():
        return None
    if not root.is_dir():
        raise ArtifactError(REASON_UNEXPECTED_ENTRY)
    return Path(os.path.realpath(root))


def _contained(root, candidate):
    """Return whether the resolved ``candidate`` stays inside ``root``."""

    try:
        resolved = os.path.realpath(candidate)
    except OSError:
        return False
    return resolved == str(root) or resolved.startswith(str(root) + os.sep)


def _digest(path):
    """Return the SHA-256 of a file without loading it all into memory."""

    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def index_artifacts(root, previous=None):
    """Return the immutable index of ``root`` as ordered entries.

    ``previous`` is the optional set of entries already published.  An
    existing version may not change its filename, size or hash on a rebuild:
    that is a mutable replacement and is rejected.  Every other problem is
    also rejected instead of being silently skipped, so a rebuild never
    publishes a half-understood directory.

    A missing artifacts directory is an empty index; it is not an error.
    """

    resolved_root = _real_directory(root)
    if resolved_root is None:
        return []

    previous_by_version = {}
    if previous is not None:
        previous_by_version = entries_by_version(previous)

    entries = []
    seen_versions = set()

    for child in sorted(Path(root).iterdir(), key=lambda item: item.name):
        version = child.name
        if not _validate_segment(version):
            raise ArtifactError(REASON_INVALID_VERSION)
        if version in seen_versions:
            raise ArtifactError(REASON_DUPLICATE_VERSION)
        if child.is_symlink():
            raise ArtifactError(REASON_PATH_ESCAPE)
        if not child.is_dir():
            raise ArtifactError(REASON_UNEXPECTED_ENTRY)
        if not _contained(resolved_root, child):
            raise ArtifactError(REASON_PATH_ESCAPE)

        members = sorted(child.iterdir(), key=lambda item: item.name)
        if not members:
            raise ArtifactError(REASON_MISSING_ARTIFACT)
        if len(members) > 1:
            # One version identifies exactly one immutable file.
            raise ArtifactError(REASON_DUPLICATE_VERSION)

        target = members[0]
        filename = target.name
        if not _validate_segment(filename):
            raise ArtifactError(REASON_PATH_ESCAPE)
        if target.is_symlink():
            raise ArtifactError(REASON_PATH_ESCAPE)
        if not target.is_file():
            raise ArtifactError(REASON_UNEXPECTED_ENTRY)
        if not _contained(resolved_root, target):
            raise ArtifactError(REASON_PATH_ESCAPE)

        entry = ArtifactEntry(
            version=version,
            filename=filename,
            size=target.stat().st_size,
            sha256=_digest(target),
        )

        prior = previous_by_version.get(version)
        if prior is not None and (
            prior.filename != entry.filename
            or prior.size != entry.size
            or prior.sha256 != entry.sha256
        ):
            raise ArtifactError(REASON_MUTABLE_REPLACEMENT)

        seen_versions.add(version)
        entries.append(entry)

    return entries


def verify_artifact(root, entry):
    """Verify ``entry``'s presence, size and digest without returning bytes.

    Raises :class:`ArtifactError` on any containment, type, size or checksum
    problem.  A caller can stream the file after this check; the file is never
    executed, imported, or extracted.
    """

    if not isinstance(entry, ArtifactEntry):
        raise TypeError("entry must be an ArtifactEntry")
    if not _validate_segment(entry.version) or not _validate_segment(
        entry.filename
    ):
        raise ArtifactError(REASON_INVALID_VERSION)

    resolved_root = _real_directory(root)
    if resolved_root is None:
        raise ArtifactError(REASON_MISSING_ARTIFACT)

    target = Path(root) / entry.version / entry.filename
    if target.is_symlink():
        raise ArtifactError(REASON_PATH_ESCAPE)
    if not target.is_file():
        raise ArtifactError(REASON_MISSING_ARTIFACT)
    if not _contained(resolved_root, target):
        raise ArtifactError(REASON_PATH_ESCAPE)
    if target.stat().st_size != entry.size:
        raise ArtifactError(REASON_CHECKSUM_MISMATCH)
    if _digest(target) != entry.sha256:
        raise ArtifactError(REASON_CHECKSUM_MISMATCH)
    return target


def serve_artifact(root, entry):
    """Return the exact bytes of ``entry`` after verifying size and SHA-256.

    The caller must already hold the trusted entry (for example the generated
    artifact index).  A file that is missing, changed, resized or replaced
    raises :class:`ArtifactError` instead of returning unverified bytes.
    """

    if not isinstance(entry, ArtifactEntry):
        raise TypeError("entry must be an ArtifactEntry")
    if not _validate_segment(entry.version):
        raise ArtifactError(REASON_INVALID_VERSION)
    if not _validate_segment(entry.filename):
        raise ArtifactError(REASON_PATH_ESCAPE)

    resolved_root = _real_directory(root)
    if resolved_root is None:
        raise ArtifactError(REASON_MISSING_ARTIFACT)

    target = Path(root) / entry.version / entry.filename
    if target.is_symlink():
        raise ArtifactError(REASON_PATH_ESCAPE)
    if not target.is_file():
        raise ArtifactError(REASON_MISSING_ARTIFACT)
    if not _contained(resolved_root, target):
        raise ArtifactError(REASON_PATH_ESCAPE)

    data = target.read_bytes()
    if len(data) != entry.size:
        raise ArtifactError(REASON_CHECKSUM_MISMATCH)
    if hashlib.sha256(data).hexdigest() != entry.sha256:
        raise ArtifactError(REASON_CHECKSUM_MISMATCH)
    return data


def entries_by_version(entries):
    """Return a ``{version: entry}`` mapping for ``entries``."""

    return {entry.version: entry for entry in entries}


def index_to_mapping(entries):
    """Return the canonical JSON mapping for an artifact index."""

    return {"artifacts": [entry.to_mapping() for entry in entries]}


def mapping_to_index(mapping):
    """Parse a persisted artifact index mapping back into entries.

    A malformed cache raises :class:`ArtifactError`; the caller decides
    whether to treat the cache as absent and rebuild it.
    """

    if not isinstance(mapping, dict):
        raise ArtifactError(REASON_INVALID_INDEX)
    raw_entries = mapping.get("artifacts")
    if not isinstance(raw_entries, list):
        raise ArtifactError(REASON_INVALID_INDEX)

    entries = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ArtifactError(REASON_INVALID_INDEX)
        try:
            version = raw["version"]
            filename = raw["filename"]
            size = raw["size"]
            sha256 = raw["sha256"]
        except KeyError:
            raise ArtifactError(REASON_INVALID_INDEX) from None
        if (
            type(version) is not str
            or type(filename) is not str
            or type(size) is not int
            or type(sha256) is not str
            or size < 0
        ):
            raise ArtifactError(REASON_INVALID_INDEX)
        entries.append(
            ArtifactEntry(
                version=version,
                filename=filename,
                size=size,
                sha256=sha256,
            )
        )
    return entries
