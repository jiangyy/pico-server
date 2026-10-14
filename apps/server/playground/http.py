from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import unquote

from apps.server.playground.catalog import PLAYGROUND_RELEASES_LOG_ID
from apps.server.playground.package import PackageError, PackageNotFound
from apps.server.protocol import logger

if TYPE_CHECKING:
    from apps.server.application import PicoRequestHandler


class _PackageResponseSent(Exception):
    pass


def dispatch_playground(handler: PicoRequestHandler, path: str) -> bool:
    route = _route(path)
    if route is None:
        return False
    try:
        action, playground_id, version_id = route
        identity = handler._authenticated_replica()
        authenticated = handler._admin_authorized() or (
            action != "publish"
            and identity is not None
            and handler.server.log_authorization is not None
            and handler.server.log_authorization.access(identity, PLAYGROUND_RELEASES_LOG_ID) == "read"
        )
        if not authenticated:
            handler._error(401, "auth.required", "valid bearer token required")
            return True
        if action == "catalog" and handler.command == "GET":
            handler._send_json(200, {
                "playgrounds": [release.to_catalog_value() for release in handler.server.playgrounds.catalog()]
            })
            return True
        if action == "publish" and handler.command == "POST":
            result = handler.server.playgrounds.publish(
                playground_id,
                handler.headers.get("X-Base-Version-Id"),
                handler.headers.get_content_type(),
                _read_package(handler),
            )
            handler._send_json(
                409 if result.status == "conflict" else (201 if result.status == "accepted" else 200),
                {"status": result.status, "version_id": result.version_id},
            )
            return True
        if action == "package" and handler.command == "GET":
            media_type, payload = handler.server.playgrounds.package(playground_id, version_id)
            handler.send_response(200)
            handler.send_header("Content-Type", media_type)
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("ETag", f'"{version_id}"')
            handler.end_headers()
            handler.wfile.write(payload)
            return True
        handler._error(405, "method.not_allowed", "method not allowed")
    except PackageNotFound as error:
        handler._error(404, "package.not_found", str(error))
    except PackageError as error:
        handler._error(400, "package.invalid", str(error))
    except _PackageResponseSent:
        pass
    except Exception:
        logger.exception("playground request failed")
        handler._error(500, "server.failure", "server failure")
    return True


def _read_package(handler: PicoRequestHandler) -> bytes:
    raw_length = handler.headers.get("Content-Length")
    if raw_length is None:
        raise PackageError("content-length is required")
    try:
        length = int(raw_length)
    except ValueError as error:
        raise PackageError("content-length is invalid") from error
    if length < 0:
        raise PackageError("content-length is invalid")
    if length > handler.server.playgrounds.max_package_bytes:
        handler._error(413, "package.too_large", "package is too large")
        raise _PackageResponseSent()
    payload = handler.rfile.read(length)
    if len(payload) != length:
        raise PackageError("request body ended early")
    return payload


def _route(path: str) -> tuple[str, str, str] | None:
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
