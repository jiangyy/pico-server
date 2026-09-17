"""Validation and in-memory decoding for Playground publishing bundles.

:func:`validate_bundle` is pure: it takes an already-decoded iterable of
:class:`BundleEntry` values and, only if the whole bundle passes, returns an
immutable mapping from normalized path to bytes.  It knows nothing about
archives, HTTP, the filesystem, the database, or Git.

:func:`decode_bundle_zip` is the single bounded bridge from an untrusted ZIP
archive to those entries.  It reads the archive exclusively in memory, rejects
unsafe member metadata before extracting anything, and never touches the
filesystem.

Validation and decoding errors carry a stable machine-readable ``reason``
code.  Error messages never contain entry content.
"""

from __future__ import annotations

import json
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from io import BytesIO
from types import MappingProxyType
from typing import Iterable, Mapping

__all__ = [
    "BundleEntry",
    "BundleValidationError",
    "BundleArchiveError",
    "validate_bundle",
    "decode_bundle_zip",
    "portable_segment_reason",
    "portable_path_collision",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SUPPORTED_RUNTIMES",
    "SUPPORTED_COMPRESSIONS",
    "MAX_ENTRY_COUNT",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "MAX_ARCHIVE_BYTES",
    "MAX_COMPRESSION_RATIO",
    "MIN_RATIO_FILE_BYTES",
    "MANIFEST_PATH",
    "REASON_CODES",
    # reason codes
    "REASON_BUNDLE_NOT_ITERABLE",
    "REASON_INVALID_ENTRY",
    "REASON_PATH_NOT_STR",
    "REASON_PATH_NUL",
    "REASON_PATH_CONTROL",
    "REASON_PATH_INVALID_UNICODE",
    "REASON_PATH_FILE_DIRECTORY",
    "REASON_PATH_PORTABLE_CHARACTER",
    "REASON_PATH_RESERVED_NAME",
    "REASON_PATH_CASE_COLLISION",
    "REASON_PATH_BACKSLASH",
    "REASON_PATH_ABSOLUTE",
    "REASON_PATH_EMPTY_SEGMENT",
    "REASON_PATH_DOT_SEGMENT",
    "REASON_PATH_FORBIDDEN",
    "REASON_PATH_NOT_ALLOWED",
    "REASON_ENTRY_KIND_NOT_FILE",
    "REASON_ENTRY_DATA_NOT_BYTES",
    "REASON_DUPLICATE_PATH",
    "REASON_TOO_MANY_ENTRIES",
    "REASON_FILE_TOO_LARGE",
    "REASON_BUNDLE_TOO_LARGE",
    "REASON_MISSING_MANIFEST",
    "REASON_MANIFEST_NOT_UTF8",
    "REASON_MANIFEST_NOT_JSON",
    "REASON_MANIFEST_NOT_OBJECT",
    "REASON_MANIFEST_MISSING_SCHEMA_VERSION",
    "REASON_MANIFEST_INVALID_SCHEMA_VERSION",
    "REASON_MANIFEST_MISSING_RUNTIME",
    "REASON_MANIFEST_INVALID_RUNTIME",
    "REASON_MANIFEST_INVALID_REQUIRES_SUBMISSION",
    # archive decoding reason codes
    "REASON_ARCHIVE_INVALID",
    "REASON_ARCHIVE_TOO_LARGE",
    "REASON_ARCHIVE_ENCRYPTED",
    "REASON_ARCHIVE_DIRECTORY",
    "REASON_ARCHIVE_LINK",
    "REASON_ARCHIVE_ENTRY_TYPE",
    "REASON_ARCHIVE_COMPRESSION",
    "REASON_ARCHIVE_DUPLICATE_NAME",
    "REASON_ARCHIVE_TOO_MANY_ENTRIES",
    "REASON_ARCHIVE_FILE_TOO_LARGE",
    "REASON_ARCHIVE_BUNDLE_TOO_LARGE",
    "REASON_ARCHIVE_SIZE_RATIO",
]

# --- Declared compatibility contract -------------------------------------
#
# The supported manifest values are deliberately narrow.  These constants are
# the single source of truth for this project's own bundle format.
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})
SUPPORTED_RUNTIMES: frozenset[str] = frozenset({"python"})

# --- Named limits ---------------------------------------------------------
MAX_ENTRY_COUNT: int = 1024
MAX_FILE_BYTES: int = 1024 * 1024
MAX_TOTAL_BYTES: int = 8 * 1024 * 1024

MANIFEST_PATH: str = "picopg.json"
ALLOWED_PREFIXES: tuple[str, ...] = ("course/", "starter/")
FORBIDDEN_ROOT_PREFIXES: tuple[str, ...] = (".picopg/", "work/")

# --- Archive decoding limits ---------------------------------------------
#
# The compressed request is bounded before the body is read so a hostile
# client cannot make the server buffer an arbitrarily large upload.  The
# per-member ratio guards against metadata that advertises a tiny compressed
# payload but expands enormously; it only applies to non-trivial members so
# small files cannot trip it.
MAX_ARCHIVE_BYTES: int = 10 * 1024 * 1024
MAX_COMPRESSION_RATIO: int = 200
MIN_RATIO_FILE_BYTES: int = 4096
SUPPORTED_COMPRESSIONS: frozenset[int] = frozenset(
    {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
)

# --- Stable reason codes --------------------------------------------------
REASON_BUNDLE_NOT_ITERABLE = "bundle_not_iterable"
REASON_INVALID_ENTRY = "invalid_entry"
REASON_PATH_NOT_STR = "path_not_str"
REASON_PATH_NUL = "path_nul"
REASON_PATH_CONTROL = "path_control"
REASON_PATH_INVALID_UNICODE = "path_invalid_unicode"
REASON_PATH_FILE_DIRECTORY = "path_file_directory"
REASON_PATH_PORTABLE_CHARACTER = "path_portable_character"
REASON_PATH_RESERVED_NAME = "path_reserved_name"
REASON_PATH_CASE_COLLISION = "path_case_collision"
REASON_PATH_BACKSLASH = "path_backslash"
REASON_PATH_ABSOLUTE = "path_absolute"
REASON_PATH_EMPTY_SEGMENT = "path_empty_segment"
REASON_PATH_DOT_SEGMENT = "path_dot_segment"
REASON_PATH_FORBIDDEN = "path_forbidden"
REASON_PATH_NOT_ALLOWED = "path_not_allowed"
REASON_ENTRY_KIND_NOT_FILE = "entry_kind_not_file"
REASON_ENTRY_DATA_NOT_BYTES = "entry_data_not_bytes"
REASON_DUPLICATE_PATH = "duplicate_path"
REASON_TOO_MANY_ENTRIES = "too_many_entries"
REASON_FILE_TOO_LARGE = "file_too_large"
REASON_BUNDLE_TOO_LARGE = "bundle_too_large"
REASON_MISSING_MANIFEST = "missing_manifest"
REASON_MANIFEST_NOT_UTF8 = "manifest_not_utf8"
REASON_MANIFEST_NOT_JSON = "manifest_not_json"
REASON_MANIFEST_NOT_OBJECT = "manifest_not_object"
REASON_MANIFEST_MISSING_SCHEMA_VERSION = "manifest_missing_schema_version"
REASON_MANIFEST_INVALID_SCHEMA_VERSION = "manifest_invalid_schema_version"
REASON_MANIFEST_MISSING_RUNTIME = "manifest_missing_runtime"
REASON_MANIFEST_INVALID_RUNTIME = "manifest_invalid_runtime"
REASON_MANIFEST_INVALID_REQUIRES_SUBMISSION = (
    "manifest_invalid_requires_submission"
)

# --- Archive decoding reason codes ----------------------------------------
#
# These are deliberately coarse and fixed: an HTTP caller receives one of
# these codes and never the archive member name, path, or content.
REASON_ARCHIVE_INVALID = "archive_invalid"
REASON_ARCHIVE_TOO_LARGE = "archive_too_large"
REASON_ARCHIVE_ENCRYPTED = "archive_encrypted"
REASON_ARCHIVE_DIRECTORY = "archive_directory"
REASON_ARCHIVE_LINK = "archive_link"
REASON_ARCHIVE_ENTRY_TYPE = "archive_entry_type"
REASON_ARCHIVE_COMPRESSION = "archive_compression"
REASON_ARCHIVE_DUPLICATE_NAME = "archive_duplicate_name"
REASON_ARCHIVE_TOO_MANY_ENTRIES = "archive_too_many_entries"
REASON_ARCHIVE_FILE_TOO_LARGE = "archive_file_too_large"
REASON_ARCHIVE_BUNDLE_TOO_LARGE = "archive_bundle_too_large"
REASON_ARCHIVE_SIZE_RATIO = "archive_size_ratio"

REASON_CODES: frozenset[str] = frozenset(
    {
        REASON_BUNDLE_NOT_ITERABLE,
        REASON_INVALID_ENTRY,
        REASON_PATH_NOT_STR,
        REASON_PATH_NUL,
        REASON_PATH_CONTROL,
        REASON_PATH_INVALID_UNICODE,
        REASON_PATH_FILE_DIRECTORY,
        REASON_PATH_PORTABLE_CHARACTER,
        REASON_PATH_RESERVED_NAME,
        REASON_PATH_CASE_COLLISION,
        REASON_PATH_BACKSLASH,
        REASON_PATH_ABSOLUTE,
        REASON_PATH_EMPTY_SEGMENT,
        REASON_PATH_DOT_SEGMENT,
        REASON_PATH_FORBIDDEN,
        REASON_PATH_NOT_ALLOWED,
        REASON_ENTRY_KIND_NOT_FILE,
        REASON_ENTRY_DATA_NOT_BYTES,
        REASON_DUPLICATE_PATH,
        REASON_TOO_MANY_ENTRIES,
        REASON_FILE_TOO_LARGE,
        REASON_BUNDLE_TOO_LARGE,
        REASON_MISSING_MANIFEST,
        REASON_MANIFEST_NOT_UTF8,
        REASON_MANIFEST_NOT_JSON,
        REASON_MANIFEST_NOT_OBJECT,
        REASON_MANIFEST_MISSING_SCHEMA_VERSION,
        REASON_MANIFEST_INVALID_SCHEMA_VERSION,
        REASON_MANIFEST_MISSING_RUNTIME,
        REASON_MANIFEST_INVALID_RUNTIME,
        REASON_MANIFEST_INVALID_REQUIRES_SUBMISSION,
        REASON_ARCHIVE_INVALID,
        REASON_ARCHIVE_TOO_LARGE,
        REASON_ARCHIVE_ENCRYPTED,
        REASON_ARCHIVE_DIRECTORY,
        REASON_ARCHIVE_LINK,
        REASON_ARCHIVE_ENTRY_TYPE,
        REASON_ARCHIVE_COMPRESSION,
        REASON_ARCHIVE_DUPLICATE_NAME,
        REASON_ARCHIVE_TOO_MANY_ENTRIES,
        REASON_ARCHIVE_FILE_TOO_LARGE,
        REASON_ARCHIVE_BUNDLE_TOO_LARGE,
        REASON_ARCHIVE_SIZE_RATIO,
    }
)


@dataclass(frozen=True)
class BundleEntry:
    """A small immutable already-decoded bundle member."""

    path: str
    data: bytes
    kind: str = "file"


class BundleValidationError(ValueError):
    """Raised when a decoded bundle violates a validation rule.

    ``reason`` is a stable code from :data:`REASON_CODES`.  The message is the
    reason code itself, so entry content can never leak through it.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class BundleArchiveError(BundleValidationError):
    """Raised when an archive cannot be safely decoded into bundle entries.

    This is a :class:`BundleValidationError` so callers that only care about
    a bounded machine-readable ``reason`` handle archives and decoded bundles
    uniformly.
    """


def _has_control_character(path: str) -> bool:
    """Return whether ``path`` contains a C0 control or DEL character."""
    return any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in path
    )


def _has_surrogate(path: str) -> bool:
    """Return whether ``path`` contains a lone surrogate code point."""
    return any(0xD800 <= ord(character) <= 0xDFFF for character in path)


# Windows reserves these device basenames at every directory level; a tree
# that uses one cannot be installed on a Windows filesystem.
_WINDOWS_FORBIDDEN_CHARACTERS = frozenset('<>:"|?*')
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


def portable_segment_reason(segment) -> str | None:
    """Return a stable reason if ``segment`` is not a portable path segment.

    This one policy is shared by Playground bundles, Work snapshots, and
    manifest generation.  It returns ``None`` for a segment that can be
    installed on both Web and Windows filesystems.
    """

    if type(segment) is not str or segment == "":
        return REASON_PATH_EMPTY_SEGMENT
    if segment in (".", ".."):
        return REASON_PATH_DOT_SEGMENT
    if "\x00" in segment:
        return REASON_PATH_NUL
    if _has_control_character(segment):
        return REASON_PATH_CONTROL
    if _has_surrogate(segment):
        return REASON_PATH_INVALID_UNICODE
    if "\\" in segment:
        return REASON_PATH_BACKSLASH
    if any(
        character in _WINDOWS_FORBIDDEN_CHARACTERS for character in segment
    ):
        return REASON_PATH_PORTABLE_CHARACTER
    if segment.endswith(" ") or segment.endswith("."):
        return REASON_PATH_PORTABLE_CHARACTER
    if segment.casefold() == ".git":
        return REASON_PATH_RESERVED_NAME
    stem = segment.rstrip(" .").split(".", 1)[0].rstrip(" ").casefold()
    if stem in _RESERVED_DEVICE_NAMES:
        return REASON_PATH_RESERVED_NAME
    return None


def portable_path_collision(paths) -> bool:
    """Return whether two complete normalized paths collide under casefold."""
    seen = set()
    for path in paths:
        folded = path.casefold()
        if folded in seen:
            return True
        seen.add(folded)
    return False


def _reject_file_directory_collision(normalized: Mapping[str, bytes]) -> None:
    """Reject trees where one path is a prefix (directory) of another."""
    for path in normalized:
        parts = path.split("/")
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in normalized:
                raise BundleValidationError(REASON_PATH_FILE_DIRECTORY)


def _normalize_path(path: object) -> str:
    """Validate and return the single normalized form of a bundle path."""

    # Exact type only: a str subclass could override __contains__ / startswith /
    # split / __eq__ and bypass every later check.
    if type(path) is not str:
        raise BundleValidationError(REASON_PATH_NOT_STR)
    if "\x00" in path:
        raise BundleValidationError(REASON_PATH_NUL)
    if _has_control_character(path):
        raise BundleValidationError(REASON_PATH_CONTROL)
    if _has_surrogate(path):
        raise BundleValidationError(REASON_PATH_INVALID_UNICODE)
    if "\\" in path:
        raise BundleValidationError(REASON_PATH_BACKSLASH)

    # Normalize Unicode exactly once; all later comparisons use this form.
    normalized = unicodedata.normalize("NFC", path)

    if normalized.startswith("/"):
        raise BundleValidationError(REASON_PATH_ABSOLUTE)

    segments = normalized.split("/")
    if any(segment == "" for segment in segments):
        raise BundleValidationError(REASON_PATH_EMPTY_SEGMENT)
    if any(segment in (".", "..") for segment in segments):
        raise BundleValidationError(REASON_PATH_DOT_SEGMENT)

    if normalized != MANIFEST_PATH:
        for prefix in FORBIDDEN_ROOT_PREFIXES:
            if normalized == prefix[:-1] or normalized.startswith(prefix):
                raise BundleValidationError(REASON_PATH_FORBIDDEN)
        for prefix in ALLOWED_PREFIXES:
            if normalized.startswith(prefix) and len(normalized) > len(prefix):
                break
        else:
            raise BundleValidationError(REASON_PATH_NOT_ALLOWED)

    for segment in segments:
        reason = portable_segment_reason(segment)
        if reason is not None:
            raise BundleValidationError(reason)

    return normalized


def _reject_json_constant(value: str) -> None:
    # json.loads otherwise accepts NaN / Infinity, which are not valid JSON.
    raise ValueError("unsupported JSON constant: " + value)


# Explicit failure sentinels.  Helper functions catch their exception entirely
# and return one of these, so the original exception (which may retain the
# offending bytes/text/message) is no longer active when BundleValidationError
# is raised: both __cause__ and __context__ stay None.
_DECODE_FAILED = object()
_PARSE_FAILED = object()
_NOT_ITERABLE = object()


def _decode_utf8(data: bytes) -> object:
    """Return decoded text, or ``_DECODE_FAILED`` with the error dropped."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return _DECODE_FAILED


def _parse_json(text: str) -> object:
    """Return parsed JSON, or ``_PARSE_FAILED`` with the error dropped."""
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except ValueError:
        return _PARSE_FAILED


def _iter_bundle(bundle: object) -> object:
    """Return an iterator, or ``_NOT_ITERABLE`` with the error dropped."""
    try:
        return iter(bundle)  # type: ignore[call-overload]
    except TypeError:
        return _NOT_ITERABLE


def _validate_manifest(data: bytes) -> None:
    """Validate the decoded manifest bytes without exposing their content."""

    text = _decode_utf8(data)
    if text is _DECODE_FAILED:
        # Raised outside any except scope: cause and context stay None.
        raise BundleValidationError(REASON_MANIFEST_NOT_UTF8)

    manifest = _parse_json(text)  # type: ignore[arg-type]
    if manifest is _PARSE_FAILED:
        # Raised outside any except scope: cause and context stay None.
        raise BundleValidationError(REASON_MANIFEST_NOT_JSON)

    if not isinstance(manifest, dict):
        raise BundleValidationError(REASON_MANIFEST_NOT_OBJECT)

    if "schemaVersion" not in manifest:
        raise BundleValidationError(REASON_MANIFEST_MISSING_SCHEMA_VERSION)
    schema_version = manifest["schemaVersion"]
    # ``type(...) is int`` deliberately rejects bool (a subclass of int),
    # float, str, None, list and dict in one check.
    if type(schema_version) is not int:
        raise BundleValidationError(REASON_MANIFEST_INVALID_SCHEMA_VERSION)
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise BundleValidationError(REASON_MANIFEST_INVALID_SCHEMA_VERSION)

    if "runtime" not in manifest:
        raise BundleValidationError(REASON_MANIFEST_MISSING_RUNTIME)
    runtime = manifest["runtime"]
    if type(runtime) is not str:
        raise BundleValidationError(REASON_MANIFEST_INVALID_RUNTIME)
    if runtime not in SUPPORTED_RUNTIMES:
        raise BundleValidationError(REASON_MANIFEST_INVALID_RUNTIME)

    if (
        "requiresSubmission" in manifest
        and type(manifest["requiresSubmission"]) is not bool
    ):
        raise BundleValidationError(
            REASON_MANIFEST_INVALID_REQUIRES_SUBMISSION
        )


def validate_bundle(bundle: Iterable[BundleEntry]) -> Mapping[str, bytes]:
    """Validate a decoded bundle and return a normalized immutable mapping.

    The bundle is consumed exactly once.  Nothing is returned unless the whole
    bundle passes.  No filesystem, database, Git, HTTP or archive behavior is
    involved.
    """

    iterator = _iter_bundle(bundle)
    if iterator is _NOT_ITERABLE:
        # Raised outside any except scope: cause and context stay None.
        raise BundleValidationError(REASON_BUNDLE_NOT_ITERABLE)

    normalized: dict[str, bytes] = {}
    total_bytes = 0
    entry_count = 0

    for entry in iterator:  # type: ignore[union-attr]
        # Exact type only: a BundleEntry subclass could lie about its fields.
        if type(entry) is not BundleEntry:
            raise BundleValidationError(REASON_INVALID_ENTRY)

        entry_count += 1
        if entry_count > MAX_ENTRY_COUNT:
            raise BundleValidationError(REASON_TOO_MANY_ENTRIES)

        # Read fields only after the exact-entry check.
        path = entry.path
        kind = entry.kind
        data = entry.data

        normalized_path = _normalize_path(path)

        if type(kind) is not str or kind != "file":
            raise BundleValidationError(REASON_ENTRY_KIND_NOT_FILE)

        if type(data) is not bytes:
            raise BundleValidationError(REASON_ENTRY_DATA_NOT_BYTES)

        size = len(data)
        if size > MAX_FILE_BYTES:
            raise BundleValidationError(REASON_FILE_TOO_LARGE)

        total_bytes += size
        if total_bytes > MAX_TOTAL_BYTES:
            raise BundleValidationError(REASON_BUNDLE_TOO_LARGE)

        if normalized_path in normalized:
            raise BundleValidationError(REASON_DUPLICATE_PATH)
        normalized[normalized_path] = data

    if MANIFEST_PATH not in normalized:
        raise BundleValidationError(REASON_MISSING_MANIFEST)

    _reject_file_directory_collision(normalized)
    if portable_path_collision(normalized):
        raise BundleValidationError(REASON_PATH_CASE_COLLISION)
    _validate_manifest(normalized[MANIFEST_PATH])

    # Sorted for a deterministic view that does not depend on input order.
    return MappingProxyType(dict(sorted(normalized.items())))


# --- In-memory ZIP decoding ------------------------------------------------
#
# Everything below converts an untrusted ZIP archive into the ``BundleEntry``
# values that :func:`validate_bundle` consumes.  Member bytes are read into
# memory; the archive is never extracted, and no member name reaches the
# filesystem.  Failures are returned as sentinel exception values and raised
# outside the ``except`` scope so no underlying error (which may quote archive
# bytes) remains chained.


def decode_bundle_zip(
    data: bytes, *, max_archive_bytes: int = MAX_ARCHIVE_BYTES
) -> list[BundleEntry]:
    """Decode a ZIP archive into entries without touching the filesystem.

    The returned list is unvalidated: callers must pass it to
    :func:`validate_bundle`.  Archive-level problems raise
    :class:`BundleArchiveError`.  ``max_archive_bytes`` bounds the compressed
    input so the same policy can be exercised in tests without large buffers.
    """

    outcome = _decode_archive(data, max_archive_bytes)
    if isinstance(outcome, BundleArchiveError):
        raise outcome
    return outcome


def _decode_archive(data: bytes, max_archive_bytes: int) -> object:
    if type(data) is not bytes:
        return BundleArchiveError(REASON_ARCHIVE_INVALID)
    if len(data) > max_archive_bytes:
        return BundleArchiveError(REASON_ARCHIVE_TOO_LARGE)

    archive = _open_archive(data)
    if archive is None:
        return BundleArchiveError(REASON_ARCHIVE_INVALID)

    try:
        return _read_archive(archive)
    except BundleArchiveError as error:
        return error
    except Exception:
        return BundleArchiveError(REASON_ARCHIVE_INVALID)
    finally:
        try:
            archive.close()
        except Exception:
            pass


def _open_archive(data: bytes):
    try:
        return zipfile.ZipFile(BytesIO(data))
    except Exception:
        return None


def _read_archive(archive) -> list[BundleEntry]:
    members = archive.infolist()
    if len(members) > MAX_ENTRY_COUNT:
        raise BundleArchiveError(REASON_ARCHIVE_TOO_MANY_ENTRIES)

    seen: set[str] = set()
    entries: list[BundleEntry] = []
    total_bytes = 0

    for info in members:
        reason = _member_metadata_reason(info)
        if reason is not None:
            raise BundleArchiveError(reason)

        if info.filename in seen:
            raise BundleArchiveError(REASON_ARCHIVE_DUPLICATE_NAME)
        seen.add(info.filename)

        if info.file_size > MAX_FILE_BYTES:
            raise BundleArchiveError(REASON_ARCHIVE_FILE_TOO_LARGE)
        total_bytes += info.file_size
        if total_bytes > MAX_TOTAL_BYTES:
            raise BundleArchiveError(REASON_ARCHIVE_BUNDLE_TOO_LARGE)
        if _is_suspicious_ratio(info):
            raise BundleArchiveError(REASON_ARCHIVE_SIZE_RATIO)

        member = _read_member(archive, info)
        if isinstance(member, BundleArchiveError):
            raise member
        entries.append(BundleEntry(info.filename, member))

    return entries


def _member_metadata_reason(info) -> str | None:
    """Return a rejection reason from member metadata, reading nothing."""

    if info.flag_bits & 0x1:
        return REASON_ARCHIVE_ENCRYPTED
    if info.is_dir() or info.filename.endswith("/"):
        return REASON_ARCHIVE_DIRECTORY
    if info.compress_type not in SUPPORTED_COMPRESSIONS:
        return REASON_ARCHIVE_COMPRESSION

    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type == stat.S_IFLNK:
        return REASON_ARCHIVE_LINK
    if file_type == stat.S_IFDIR:
        return REASON_ARCHIVE_DIRECTORY
    if file_type not in (0, stat.S_IFREG):
        return REASON_ARCHIVE_ENTRY_TYPE
    return None


def _is_suspicious_ratio(info) -> bool:
    if info.compress_size <= 0:
        return False
    if info.file_size < MIN_RATIO_FILE_BYTES:
        return False
    return info.file_size > info.compress_size * MAX_COMPRESSION_RATIO


def _read_member(archive, info) -> object:
    """Read one member, bounded to the per-file limit, or return an error."""

    try:
        with archive.open(info) as member:
            data = member.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                return BundleArchiveError(REASON_ARCHIVE_FILE_TOO_LARGE)
            if member.read(1):
                return BundleArchiveError(REASON_ARCHIVE_FILE_TOO_LARGE)
    except BundleArchiveError as error:
        return error
    except Exception:
        return BundleArchiveError(REASON_ARCHIVE_INVALID)
    return data
