from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, urlsplit

from apps.server.authorization import PrincipalSession
from apps.server.comments import CommentError, CommentPublisher
from apps.server.playgrounds import (
    PLAYGROUND_RELEASES_LOG_ID,
    FilePackageStore,
    PackageAdapter,
    PackageError,
    PackageNotFound,
    PlaygroundPublisher,
)
from apps.server.protocol import LogBinding, ReplicaHttpServer, ReplicaRequestHandler
from apps.server.workers import (
    DraftRequestConsumer,
    ServerWorkerThread,
    SubmissionRequestConsumer,
    SubmissionTagWriter,
    draft_request_logs,
    is_draft_request_log,
    is_submission_request_log,
    submission_request_logs,
)
from picopg.api import InvalidReplicaData, ReplicaLogStore


class PicoPGHttpServer(ReplicaHttpServer):
    def __init__(
        self,
        address: tuple[str, int],
        store: ReplicaLogStore,
        token: str,
        package_root: Path,
        bindings: Iterable[LogBinding] | None = None,
        package_adapter: PackageAdapter | None = None,
        principal_sessions: Iterable[PrincipalSession] = (),
        submission_tag_writer: SubmissionTagWriter | None = None,
    ) -> None:
        sessions = tuple(principal_sessions)
        if any(session.grants.get(PLAYGROUND_RELEASES_LOG_ID) == "append" for session in sessions):
            raise ValueError(f"{PLAYGROUND_RELEASES_LOG_ID!r} is server-owned and read-only")
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
            principal_sessions=sessions,
        )
        self.playgrounds = PlaygroundPublisher(
            self, FilePackageStore(package_root), package_adapter
        )
        self.comments = CommentPublisher(self)
        worker_bindings = dict(self.bindings)
        request_logs = tuple(sorted(set(draft_request_logs(worker_bindings)) | {
            log_id for log_id in self.store.log_ids() if is_draft_request_log(log_id)
        }))
        self.draft_consumer = DraftRequestConsumer(
            self.store,
            request_logs,
            lambda log_id, head: self.broadcast(log_id, head),
        )
        consumers = [self.draft_consumer]
        self.submission_consumer = None
        if submission_tag_writer is not None:
            submission_logs = tuple(sorted(set(submission_request_logs(worker_bindings)) | {
                log_id for log_id in self.store.log_ids() if is_submission_request_log(log_id)
            }))
            self.submission_consumer = SubmissionRequestConsumer(
                self.store,
                submission_logs,
                submission_tag_writer,
                lambda log_id, head: self.broadcast(log_id, head),
            )
            consumers.append(self.submission_consumer)
        self.workers = ServerWorkerThread(tuple(consumers)) if consumers else None
        if self.workers is not None:
            self.workers.start()

    def append_and_broadcast(self, log_id, expected_head, records):
        result = super().append_and_broadcast(log_id, expected_head, records)
        if result.kind == "accepted" and self.workers is not None:
            if is_draft_request_log(log_id):
                self.draft_consumer.add_request_log(log_id)
            if self.submission_consumer is not None and is_submission_request_log(log_id):
                self.submission_consumer.add_request_log(log_id)
            self.workers.notify_append(log_id)
        return result

    def server_close(self) -> None:
        if self.workers is not None:
            self.workers.close()
        super().server_close()


class PicoRequestHandler(ReplicaRequestHandler):
    server: PicoPGHttpServer

    def _dispatch(self) -> None:
        path = urlsplit(self.path).path
        comment_scope = _comment_route(path)
        if comment_scope is not None:
            self._dispatch_comment(*comment_scope)
            return
        route = _playground_route(path)
        if route is None:
            super()._dispatch()
            return
        try:
            action, playground_id, version_id = route
            identity = self._authenticated_replica()
            authenticated = self._admin_authorized() or (
                action != "publish"
                and identity is not None
                and self.server.log_authorization is not None
                and self.server.log_authorization.access(identity, PLAYGROUND_RELEASES_LOG_ID) == "read"
            )
            if not authenticated:
                self._error(401, "auth.required", "valid bearer token required")
                return
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

    def _dispatch_comment(self, principal_id: str, draft_id: str) -> None:
        """Operator-only: append a fact to the Draft's server-owned comment Log."""
        try:
            if not self._admin_authorized():
                self._error(401, "auth.required", "valid operator token required")
                return
            if self.command != "POST":
                self._error(405, "method.not_allowed", "method not allowed")
                return
            body = self._read_json()
            if not isinstance(body, dict) or set(body) != {"author", "text"}:
                raise CommentError("comment body must contain author and text")
            position = self.server.comments.add(principal_id, draft_id, body["author"], body["text"])
            self._send_json(201, {"position": position.to_value()})
        except (CommentError, InvalidReplicaData) as error:
            self._error(400, "comment.invalid", str(error))
        except _ResponseSent:
            pass
        except Exception:
            from apps.server.protocol import logger

            logger.exception("comment request failed")
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
        if length > self.server.playgrounds.max_package_bytes:
            self._error(413, "package.too_large", "package is too large")
            raise _PackageResponseSent()
        payload = self.rfile.read(length)
        if len(payload) != length:
            raise PackageError("request body ended early")
        return payload


class _PackageResponseSent(Exception):
    pass


def _comment_route(path: str) -> tuple[str, str] | None:
    parts = path.split("/")
    if (
        parts[:4] != ["", "api", "v1", "principals"]
        or len(parts) != 8
        or parts[5] != "drafts"
        or parts[7] != "comments"
    ):
        return None
    principal_id = unquote(parts[4])
    draft_id = unquote(parts[6])
    return (principal_id, draft_id) if principal_id and draft_id else None


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
