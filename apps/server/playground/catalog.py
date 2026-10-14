from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from apps.server.playground.package import (
    FilePackageStore,
    PackageAdapter,
    PackageError,
    PackageNotFound,
    ZipPackageAdapter,
    validate_playground_id,
    validate_version_id,
    version_id,
)
from picopg.api import AppendResult, Position, ReplicaLogStore, ReplicatedRecord, record_id_of


PLAYGROUND_RELEASES_LOG_ID = "playground-releases"


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

    @property
    def max_package_bytes(self) -> int:
        return self._adapter.max_bytes

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
        playground_id = validate_playground_id(playground_id)
        if media_type != self._adapter.media_type:
            raise PackageError(f"unsupported package media type: {media_type}")
        info = self._adapter.inspect(payload)
        new_version = version_id(payload)

        with self._lock:
            current = next((
                item for item in self.catalog()
                if item.playground_id == playground_id
            ), None)
            if current is not None and current.version_id == new_version:
                return PublishResult("no_op", new_version)
            current_version = None if current is None else current.version_id
            if base_version_id != current_version:
                return PublishResult("conflict", current_version or "")

            self._packages.put(playground_id, new_version, payload)
            parent = self._store.head(PLAYGROUND_RELEASES_LOG_ID)
            data: dict[str, object] = {
                "type": "playground.released",
                "playgroundId": playground_id,
                "versionId": new_version,
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
            return PublishResult("accepted", new_version)

    def package(self, playground_id: str, requested_version: str) -> tuple[str, bytes]:
        playground_id = validate_playground_id(playground_id)
        validate_version_id(requested_version)
        release = next((
            item for item in self._releases()
            if item.playground_id == playground_id and item.version_id == requested_version
        ), None)
        if release is None:
            raise PackageNotFound("package release not found")
        return release.media_type, self._packages.read(playground_id, requested_version)

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
            if not page.records:
                raise PackageError("release Log returned an empty continuation page")
            after = page.records[-1].position


def _release(data: object) -> PlaygroundRelease | None:
    if not isinstance(data, Mapping) or data.get("type") != "playground.released":
        return None
    media_type = data.get("mediaType")
    title = data.get("title")
    if not isinstance(media_type, str) or not media_type:
        raise PackageError("release mediaType is invalid")
    if title is not None and not isinstance(title, str):
        raise PackageError("release title is invalid")
    return PlaygroundRelease(
        validate_playground_id(data.get("playgroundId")),
        validate_version_id(data.get("versionId")),
        media_type,
        title,
    )
