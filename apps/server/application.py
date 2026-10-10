from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, urlsplit

from apps.server.playgrounds import (
    MAX_ARCHIVE_BYTES,
    PLAYGROUND_RELEASES_LOG_ID,
    FilePackageStore,
    PackageError,
    PackageNotFound,
    PlaygroundPublisher,
)
from apps.server.protocol import LogBinding, ReplicaHttpServer, ReplicaRequestHandler
from picopg.api import ReplicaLogStore


class PicoPGHttpServer(ReplicaHttpServer):
    def __init__(
        self,
        address: tuple[str, int],
        store: ReplicaLogStore,
        token: str,
        package_root: Path,
        bindings: Iterable[LogBinding] | None = None,
    ) -> None:
        exposed = list(bindings or ())
        release_access = next(
            (
                access
                for log_id, access in exposed
                if log_id == PLAYGROUND_RELEASES_LOG_ID
            ),
            None,
        )
        if release_access is None:
            exposed.append((PLAYGROUND_RELEASES_LOG_ID, "read"))
        elif release_access != "read":
            raise ValueError(
                f"{PLAYGROUND_RELEASES_LOG_ID!r} is server-owned and must be read-only"
            )
        super().__init__(
            address,
            store,
            token,
            bindings=exposed,
            handler_class=PicoRequestHandler,
        )
        self.playgrounds = PlaygroundPublisher(self, FilePackageStore(package_root))


class PicoRequestHandler(ReplicaRequestHandler):
    server: PicoPGHttpServer

    def _dispatch(self) -> None:
        route = _playground_route(urlsplit(self.path).path)
        if route is None:
            super()._dispatch()
            return
        try:
            if not self._authorized():
                self._error(401, "auth.required", "valid bearer token required")
                return
            action, playground_id, version_id = route
            if action == "catalog" and self.command == "GET":
                self._send_json(
                    200,
                    {
                        "playgrounds": [
                            release.to_catalog_value()
                            for release in self.server.playgrounds.catalog()
                        ]
                    },
                )
                return
            if action == "publish" and self.command == "POST":
                result = self.server.playgrounds.publish(
                    playground_id,
                    self.headers.get("X-Base-Version-Id"),
                    self.headers.get_content_type(),
                    self._read_package(),
                )
                self._send_json(
                    409 if result.status == "conflict" else (
                        201 if result.status == "accepted" else 200
                    ),
                    {"status": result.status, "version_id": result.version_id},
                )
                return
            if action == "package" and self.command == "GET":
                media_type, payload = self.server.playgrounds.package(
                    playground_id, version_id
                )
                self.send_response(200)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("ETag", f'"{version_id}"')
                self.end_headers()
                self.wfile.write(payload)
                return
            self._error(405, "method.not_allowed", "method not allowed")
        except PackageNotFound as error:
            self._error(404, "package.not_found", str(error))
        except PackageError as error:
            self._error(400, "package.invalid", str(error))
        except _PackageResponseSent:
            pass
        except Exception:
            from apps.server.protocol import logger

            logger.exception("playground request failed")
            self._error(500, "server.failure", "server failure")

    def _read_package(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise PackageError("content-length is required")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise PackageError("content-length is invalid") from error
        if length < 0:
            raise PackageError("content-length is invalid")
        if length > MAX_ARCHIVE_BYTES:
            self._error(413, "package.too_large", "package is larger than 10 MiB")
            raise _PackageResponseSent()
        payload = self.rfile.read(length)
        if len(payload) != length:
            raise PackageError("request body ended early")
        return payload


class _PackageResponseSent(Exception):
    pass


def _playground_route(path: str) -> tuple[str, str, str] | None:
    if path == "/api/v1/catalog":
        return "catalog", "", ""
    parts = path.split("/")
    if parts[:4] != ["", "api", "v1", "playgrounds"]:
        return None
    if len(parts) == 6 and parts[5] == "versions":
        playground_id = unquote(parts[4])
        return None if not playground_id else ("publish", playground_id, "")
    if len(parts) == 8 and parts[5] == "versions" and parts[7] == "package":
        playground_id = unquote(parts[4])
        version_id = unquote(parts[6])
        if playground_id and version_id:
            return "package", playground_id, version_id
    return None
