from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4


ZIP_MEDIA_TYPE = "application/zip"
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_ENTRIES = 1024
MAX_COMPRESSION_RATIO = 200

_PLAYGROUND_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_VERSION_ID = re.compile(r"sha256:([0-9a-f]{64})")


class PackageError(ValueError):
    pass


class PackageNotFound(FileNotFoundError):
    pass


@dataclass(frozen=True, slots=True)
class PackageInfo:
    media_type: str
    title: str | None


class PackageAdapter(Protocol):
    media_type: str
    max_bytes: int

    def inspect(self, payload: bytes) -> PackageInfo: ...


class ZipPackageAdapter:
    media_type = ZIP_MEDIA_TYPE
    max_bytes = MAX_ARCHIVE_BYTES

    def inspect(self, payload: bytes) -> PackageInfo:
        if len(payload) > MAX_ARCHIVE_BYTES:
            raise PackageError("package is larger than 10 MiB")
        try:
            archive = zipfile.ZipFile(BytesIO(payload))
        except (OSError, zipfile.BadZipFile) as error:
            raise PackageError("package is not a valid ZIP archive") from error

        manifest_bytes: bytes | None = None
        seen: set[str] = set()
        folded: set[str] = set()
        total = 0
        try:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES:
                raise PackageError("package has too many entries")
            for entry in entries:
                name = _package_path(entry.filename)
                if name in seen or name.casefold() in folded:
                    raise PackageError(f"duplicate package path: {name}")
                seen.add(name)
                folded.add(name.casefold())
                if entry.flag_bits & 0x1:
                    raise PackageError(f"encrypted package entry: {name}")
                if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise PackageError(f"unsupported compression for {name}")
                mode = (entry.external_attr >> 16) & 0o170000
                if entry.is_dir() or mode == stat.S_IFDIR:
                    raise PackageError(f"directory entry is not allowed: {name}")
                if mode not in (0, stat.S_IFREG):
                    raise PackageError(f"non-regular package entry: {name}")
                if entry.file_size > MAX_FILE_BYTES:
                    raise PackageError(f"package entry is too large: {name}")
                total += entry.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise PackageError("package expands beyond 8 MiB")
                if entry.file_size >= 4096 and entry.file_size > max(entry.compress_size, 1) * MAX_COMPRESSION_RATIO:
                    raise PackageError(f"suspicious compression ratio: {name}")
                content = archive.read(entry)
                if len(content) != entry.file_size:
                    raise PackageError(f"package entry size changed: {name}")
                if name == "picopg.json":
                    manifest_bytes = content
        except (OSError, RuntimeError, zipfile.BadZipFile) as error:
            raise PackageError("package contains an unreadable entry") from error
        finally:
            archive.close()

        if manifest_bytes is None:
            raise PackageError("package is missing picopg.json")
        if "course/index.html" not in seen:
            raise PackageError("package is missing course/index.html")
        return PackageInfo(ZIP_MEDIA_TYPE, _manifest_title(manifest_bytes, seen))


class FilePackageStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, playground_id: str, expected_version: str, payload: bytes) -> None:
        path = self._path(playground_id, expected_version)
        if path.exists():
            if version_id(path.read_bytes()) != expected_version:
                raise PackageError(f"stored package is corrupt: {expected_version}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{uuid4().hex}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        except OSError:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def read(self, playground_id: str, expected_version: str) -> bytes:
        path = self._path(playground_id, expected_version)
        try:
            payload = path.read_bytes()
        except FileNotFoundError as error:
            raise PackageNotFound("package not found") from error
        if version_id(payload) != expected_version:
            raise PackageError(f"stored package is corrupt: {expected_version}")
        return payload

    def _path(self, playground_id: str, expected_version: str) -> Path:
        validate_playground_id(playground_id)
        digest = version_digest(expected_version)
        return self._root / playground_id / digest


def validate_playground_id(value: object) -> str:
    if not isinstance(value, str) or _PLAYGROUND_ID.fullmatch(value) is None:
        raise PackageError("playground id must be a lowercase slug")
    return value


def version_id(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def validate_version_id(value: object) -> str:
    if not isinstance(value, str):
        raise PackageError("version id is invalid")
    version_digest(value)
    return value


def version_digest(value: str) -> str:
    match = _VERSION_ID.fullmatch(value)
    if match is None:
        raise PackageError("version id must be sha256:<hex>")
    return match.group(1)


def _package_path(raw: str) -> str:
    if not raw or "\\" in raw or "\x00" in raw:
        raise PackageError("package path is invalid")
    normalized = unicodedata.normalize("NFC", raw)
    if normalized != raw or any(ord(character) < 32 for character in raw):
        raise PackageError(f"package path is not portable: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or str(path) != raw or any(part in ("", ".", "..") for part in path.parts):
        raise PackageError(f"unsafe package path: {raw!r}")
    name = str(path)
    if name == "picopg.json" or name.startswith("course/") or name.startswith("starter/"):
        return name
    raise PackageError(f"unexpected package path: {name}")


def _manifest_title(payload: bytes, paths: set[str]) -> str | None:
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageError("picopg.json is invalid JSON") from error
    if not isinstance(value, dict):
        raise PackageError("picopg.json must be an object")
    if value.get("schemaVersion") != 1:
        raise PackageError("picopg.json schemaVersion must be 1")
    if value.get("runtime") != "python":
        raise PackageError("picopg.json runtime must be python")
    module = value.get("module")
    if not isinstance(module, str) or not module.endswith(".js"):
        raise PackageError("picopg.json module must be a JavaScript package path")
    module = _package_path(module)
    if not module.startswith("course/") or module not in paths:
        raise PackageError("picopg.json module is missing from the package")
    title = value.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise PackageError("picopg.json title must be a non-empty string")
    return title


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass
