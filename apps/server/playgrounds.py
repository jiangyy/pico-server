from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import unicodedata
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4

from picopg.api import (
    AppendResult,
    Position,
    ReplicaLogStore,
    ReplicatedRecord,
    record_id_of,
)


PLAYGROUND_RELEASES_LOG_ID = "playground-releases"
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

    def inspect(self, payload: bytes) -> PackageInfo: ...


class PublishServer(Protocol):
    store: ReplicaLogStore

    def append_and_broadcast(
        self,
        log_id: str,
        expected_head: Position | None,
        records: tuple[ReplicatedRecord, ...],
    ) -> AppendResult: ...


@dataclass(frozen=True, slots=True)
class PlaygroundRelease:
    playground_id: str
    version_id: str
    media_type: str
    title: str | None

    def to_catalog_value(self) -> dict[str, object]:
        value: dict[str, object] = {
            "playground_id": self.playground_id,
            "current_version_id": self.version_id,
            "media_type": self.media_type,
        }
        if self.title is not None:
            value["title"] = self.title
        return value


@dataclass(frozen=True, slots=True)
class PublishResult:
    status: str
    version_id: str


class ZipPackageAdapter:
    media_type = ZIP_MEDIA_TYPE

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
                if (
                    entry.file_size >= 4096
                    and entry.file_size
                    > max(entry.compress_size, 1) * MAX_COMPRESSION_RATIO
                ):
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
        return PackageInfo(ZIP_MEDIA_TYPE, _manifest_title(manifest_bytes))


class FilePackageStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, playground_id: str, version_id: str, payload: bytes) -> None:
        path = self._path(playground_id, version_id)
        if path.exists():
            if _version_id(path.read_bytes()) != version_id:
                raise PackageError(f"stored package is corrupt: {version_id}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{uuid4().hex}.tmp"
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
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

    def read(self, playground_id: str, version_id: str) -> bytes:
        path = self._path(playground_id, version_id)
        try:
            payload = path.read_bytes()
        except FileNotFoundError as error:
            raise PackageNotFound("package not found") from error
        if _version_id(payload) != version_id:
            raise PackageError(f"stored package is corrupt: {version_id}")
        return payload

    def _path(self, playground_id: str, version_id: str) -> Path:
        _playground_id(playground_id)
        digest = _version_digest(version_id)
        return self._root / playground_id / digest


class PlaygroundPublisher:
    def __init__(
        self,
        server: PublishServer,
        packages: FilePackageStore,
        adapter: PackageAdapter | None = None,
    ) -> None:
        self._server = server
        self._store = server.store
        self._packages = packages
        self._adapter = adapter or ZipPackageAdapter()
        self._lock = threading.Lock()

    def catalog(self) -> tuple[PlaygroundRelease, ...]:
        current: dict[str, PlaygroundRelease] = {}
        for release in self._releases():
            current[release.playground_id] = release
        return tuple(current[key] for key in sorted(current))

    def publish(
        self,
        playground_id: str,
        base_version_id: str | None,
        media_type: str,
        payload: bytes,
    ) -> PublishResult:
        playground_id = _playground_id(playground_id)
        if media_type != self._adapter.media_type:
            raise PackageError(f"unsupported package media type: {media_type}")
        info = self._adapter.inspect(payload)
        version_id = _version_id(payload)

        with self._lock:
            current = next(
                (
                    item
                    for item in self.catalog()
                    if item.playground_id == playground_id
                ),
                None,
            )
            if current is not None and current.version_id == version_id:
                return PublishResult("no_op", version_id)
            current_version = None if current is None else current.version_id
            if base_version_id != current_version:
                return PublishResult("conflict", current_version or "")

            self._packages.put(playground_id, version_id, payload)
            parent = self._store.head(PLAYGROUND_RELEASES_LOG_ID)
            data: dict[str, object] = {
                "type": "playground.released",
                "playgroundId": playground_id,
                "versionId": version_id,
                "mediaType": info.media_type,
            }
            if info.title is not None:
                data["title"] = info.title
            record = ReplicatedRecord(
                id=record_id_of(PLAYGROUND_RELEASES_LOG_ID, parent, (), data),
                log_id=PLAYGROUND_RELEASES_LOG_ID,
                parent=parent,
                requires=(),
                data=data,
            )
            result = self._server.append_and_broadcast(
                PLAYGROUND_RELEASES_LOG_ID, parent, (record,)
            )
            if result.kind != "accepted":
                raise RuntimeError("release Log changed during a locked publish")
            return PublishResult("accepted", version_id)

    def package(
        self, playground_id: str, version_id: str
    ) -> tuple[str, bytes]:
        playground_id = _playground_id(playground_id)
        _version_digest(version_id)
        release = next(
            (
                item
                for item in self._releases()
                if item.playground_id == playground_id
                and item.version_id == version_id
            ),
            None,
        )
        if release is None:
            raise PackageNotFound("package release not found")
        return release.media_type, self._packages.read(playground_id, version_id)

    def _releases(self) -> tuple[PlaygroundRelease, ...]:
        releases: list[PlaygroundRelease] = []
        after: Position | None = None
        while True:
            page = self._store.read(PLAYGROUND_RELEASES_LOG_ID, after, 128)
            for record in page.records:
                release = _release(record.data)
                if release is not None:
                    releases.append(release)
            if not page.has_more:
                return tuple(releases)
            after = page.records[-1].position


def _package_path(raw: str) -> str:
    if not raw or "\\" in raw or "\x00" in raw:
        raise PackageError("package path is invalid")
    normalized = unicodedata.normalize("NFC", raw)
    if normalized != raw or any(ord(character) < 32 for character in raw):
        raise PackageError(f"package path is not portable: {raw!r}")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or str(path) != raw
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise PackageError(f"unsafe package path: {raw!r}")
    name = str(path)
    if name == "picopg.json" or name.startswith("course/") or name.startswith("starter/"):
        return name
    raise PackageError(f"unexpected package path: {name}")


def _manifest_title(payload: bytes) -> str | None:
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
    title = value.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise PackageError("picopg.json title must be a non-empty string")
    return title


def _release(data: object) -> PlaygroundRelease | None:
    if not isinstance(data, Mapping) or data.get("type") != "playground.released":
        return None
    playground_id = data.get("playgroundId")
    version_id = data.get("versionId")
    media_type = data.get("mediaType")
    title = data.get("title")
    if not isinstance(media_type, str) or not media_type:
        raise PackageError("release mediaType is invalid")
    if title is not None and not isinstance(title, str):
        raise PackageError("release title is invalid")
    return PlaygroundRelease(
        _playground_id(playground_id),
        _version_id_value(version_id),
        media_type,
        title,
    )


def _playground_id(value: object) -> str:
    if not isinstance(value, str) or _PLAYGROUND_ID.fullmatch(value) is None:
        raise PackageError("playground id must be a lowercase slug")
    return value


def _version_id(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _version_id_value(value: object) -> str:
    if not isinstance(value, str):
        raise PackageError("version id is invalid")
    _version_digest(value)
    return value


def _version_digest(version_id: str) -> str:
    match = _VERSION_ID.fullmatch(version_id)
    if match is None:
        raise PackageError("version id must be sha256:<hex>")
    return match.group(1)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass
