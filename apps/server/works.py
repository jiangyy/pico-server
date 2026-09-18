"""Portable file sets a client hands in.

A submission carries the student's own files and nothing else: this module owns
the one definition of what such a file set may look like.  Only paths under
``work/`` are accepted, so a client can never contribute ``course/**``,
``starter/**``, ``picopg.json`` or server-owned metadata, and entry count,
per-file bytes and total bytes are bounded.  Nothing is returned unless the
whole file set passes, so a caller never validates half of a snapshot.
"""

import unicodedata
from dataclasses import dataclass
from types import MappingProxyType

import bundles


__all__ = [
    "WorkEntry",
    "WorkError",
    "WorkValidationError",
    "validate_work_files",
    "MAX_WORK_ENTRY_COUNT",
    "MAX_WORK_FILE_BYTES",
    "MAX_WORK_TOTAL_BYTES",
    "WORK_PREFIX",
    "REASON_CODES",
]



# --- Named server limits ---------------------------------------------------
#
# A submitted file set is bounded before anything is decoded, stored, or read
# back, so one request can never be made arbitrarily expensive.
MAX_WORK_ENTRY_COUNT = 1024
MAX_WORK_FILE_BYTES = 1024 * 1024
MAX_WORK_TOTAL_BYTES = 8 * 1024 * 1024

# The only prefix a client may contribute under.
WORK_PREFIX = "work/"

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
REASON_PATH_PORTABLE_CHARACTER = "path_portable_character"
REASON_PATH_RESERVED_NAME = "path_reserved_name"
REASON_PATH_CASE_COLLISION = "path_case_collision"
REASON_ENTRY_KIND_NOT_FILE = "entry_kind_not_file"
REASON_ENTRY_DATA_NOT_BYTES = "entry_data_not_bytes"
REASON_DUPLICATE_PATH = "duplicate_path"
REASON_TOO_MANY_ENTRIES = "too_many_entries"
REASON_FILE_TOO_LARGE = "file_too_large"
REASON_WORK_TOO_LARGE = "work_too_large"

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
        REASON_PATH_PORTABLE_CHARACTER,
        REASON_PATH_RESERVED_NAME,
        REASON_PATH_CASE_COLLISION,
        REASON_ENTRY_KIND_NOT_FILE,
        REASON_ENTRY_DATA_NOT_BYTES,
        REASON_DUPLICATE_PATH,
        REASON_TOO_MANY_ENTRIES,
        REASON_FILE_TOO_LARGE,
        REASON_WORK_TOO_LARGE,
    }
)


class WorkError(Exception):
    """Raised when a submitted file set cannot be accepted.

    ``reason`` is a stable code from :data:`REASON_CODES`.  The message is the
    reason code itself, so no path or content leaks through it.
    """

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class WorkValidationError(WorkError):
    """A submitted file set violates the portable-path contract."""


@dataclass(frozen=True)
class WorkEntry:
    """One immutable file of a submitted set, already decoded."""

    path: str
    data: bytes
    kind: str = "file"


# Maps the shared portable-path reason codes (owned by ``bundles``) to the
# Work domain's reason codes.  Only one policy implementation exists.
_PORTABLE_REASON_MAP = {
    bundles.REASON_PATH_NUL: REASON_PATH_NUL,
    bundles.REASON_PATH_CONTROL: REASON_PATH_CONTROL_CHARACTER,
    bundles.REASON_PATH_INVALID_UNICODE: REASON_PATH_INVALID_UNICODE,
    bundles.REASON_PATH_BACKSLASH: REASON_PATH_BACKSLASH,
    bundles.REASON_PATH_EMPTY_SEGMENT: REASON_PATH_EMPTY_SEGMENT,
    bundles.REASON_PATH_DOT_SEGMENT: REASON_PATH_DOT_SEGMENT,
    bundles.REASON_PATH_PORTABLE_CHARACTER: REASON_PATH_PORTABLE_CHARACTER,
    bundles.REASON_PATH_RESERVED_NAME: REASON_PATH_RESERVED_NAME,
}


def _normalize_work_path(path):
    """Return the one normalized ``work/**`` path, or raise a reason."""

    if type(path) is not str:
        raise WorkValidationError(REASON_PATH_NOT_STR)

    normalized = unicodedata.normalize("NFC", path)

    if normalized.startswith("/"):
        raise WorkValidationError(REASON_PATH_ABSOLUTE)
    segments = normalized.split("/")
    if any(segment == "" for segment in segments):
        raise WorkValidationError(REASON_PATH_EMPTY_SEGMENT)
    if any(segment in (".", "..") for segment in segments):
        raise WorkValidationError(REASON_PATH_DOT_SEGMENT)

    # Apply the shared portable-segment policy from ``bundles`` before the Work
    # allowlist so a non-portable shape reports its precise reason.  A Work
    # tree must install on both Web and Windows filesystems.
    for segment in segments:
        reason = bundles.portable_segment_reason(segment)
        if reason is not None:
            raise WorkValidationError(
                _PORTABLE_REASON_MAP.get(reason, REASON_PATH_NOT_ALLOWED)
            )

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
    """Validate a submitted file set and return a normalized mapping.

    Only paths under ``work/`` are accepted, so a client can never contribute
    ``.picopg/**``, ``course/**``, ``starter/**`` or ``picopg.json``.  Entry
    count, per-file bytes, and total bytes are bounded by the named constants.
    Nothing is returned unless the whole set passes.
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

    if bundles.portable_path_collision(normalized):
        raise WorkValidationError(REASON_PATH_CASE_COLLISION)

    # Sorted for a deterministic view that does not depend on input order.
    return MappingProxyType(dict(sorted(normalized.items())))
