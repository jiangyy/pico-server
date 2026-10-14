from __future__ import annotations

import hmac
import json
import logging
import queue
import threading
from collections.abc import Callable, Iterable, Mapping
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NoReturn
from urllib.parse import parse_qs, unquote, urlsplit

from picopg.api import (
    AppendResult,
    InvalidReplicaData,
    MissingDependency,
    Position,
    ReplicaLogError,
    ReplicaLogStore,
    ReplicatedRecord,
    UnknownPosition,
)
from apps.server.authentication.model import (
    AuthenticatedReplica,
    Authenticator,
    LogAuthorization,
    PrincipalSession,
    PrincipalLogAuthorization,
    StaticAuthenticator,
    StaticReplicaDirectory,
    permits_log,
)


MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_PAGE_RECORDS = 128
PROTOCOL_VERSION = 1
HANDSHAKE_CAPABILITIES = ("replicate",)
SESSION_COOKIE = "picopg_session"
OPERATOR_SESSION_COOKIE = "picopg_operator_session"
logger = logging.getLogger("picopg.server")

LogAccess = str
LogBinding = tuple[str, LogAccess]
_READ_ACTIONS = {"head", "records", "has"}


class ReplicaHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    @staticmethod
    def _compile_bindings(
        bindings: Iterable[LogBinding] | None,
    ) -> Mapping[str, LogAccess]:
        if bindings is None:
            return {}
        compiled: dict[str, LogAccess] = {}
        for log_id, access in bindings:
            if not isinstance(log_id, str) or not log_id:
                raise ValueError("binding logId must be a non-empty string")
            if access not in ("read", "append"):
                raise ValueError(
                    f"binding access for {log_id!r} must be 'read' or 'append', got {access!r}"
                )
            if log_id in compiled:
                raise ValueError(f"duplicate binding for {log_id!r}")
            compiled[log_id] = access
        return compiled

    def __init__(
        self,
        address: tuple[str, int],
        store: ReplicaLogStore,
        token: str,
        bindings: Iterable[LogBinding] | None = None,
        handler_class: type[BaseHTTPRequestHandler] | None = None,
        principal_sessions: Iterable[PrincipalSession] = (),
        authenticator: Authenticator | None = None,
        log_authorization: LogAuthorization | None = None,
        operator_authenticator: Callable[[str], bool] | None = None,
    ) -> None:
        if not token:
            raise ValueError("replica token must be non-empty")
        self.store = store
        self.replica_token = token
        self.bindings: Mapping[str, LogAccess] = self._compile_bindings(bindings)
        sessions = tuple(principal_sessions)
        if sessions and (authenticator is not None or log_authorization is not None):
            raise ValueError("principal_sessions cannot be combined with explicit authentication")
        self.authenticator = authenticator or (StaticAuthenticator(sessions) if sessions else None)
        directory = StaticReplicaDirectory(sessions)
        self.log_authorization = log_authorization or (
            PrincipalLogAuthorization(sessions, directory) if sessions else None
        )
        self.operator_authenticator = operator_authenticator
        self._subscriber_lock = threading.Lock()
        self._subscribers: list[tuple[queue.Queue[str], Callable[[str], bool]]] = []
        super().__init__(address, handler_class or ReplicaRequestHandler)

    def broadcast(self, log_id: str, head: "Position | None") -> None:
        event = json.dumps(
            {
                "logId": log_id,
                "head": None if head is None else head.to_value(),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        with self._subscriber_lock:
            queues = list(self._subscribers)
        for subscriber, observes in queues:
            if not observes(log_id):
                continue
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                logger.warning("subscriber queue is full, dropping event for %s", log_id)

    def append_and_broadcast(
        self,
        log_id: str,
        expected_head: Position | None,
        records: tuple[ReplicatedRecord, ...],
    ) -> AppendResult:
        result = self.store.append(log_id, expected_head, records)
        if result.kind == "accepted":
            self.broadcast(log_id, result.head)
        return result


class ReplicaRequestHandler(BaseHTTPRequestHandler):
    server: ReplicaHttpServer

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            url = urlsplit(self.path)
            if url.path == "/healthz" and self.command in ("GET", "HEAD"):
                self._send_json(200, {"status": "ok"})
                return
            if url.path == "/api/v1/replica/handshake":
                if self.command != "GET":
                    self._error(405, "method.not_allowed", "method not allowed")
                    return
                self._send_json(
                    200,
                    {
                        "protocol": PROTOCOL_VERSION,
                        "server": "picopg-server",
                        "capabilities": list(HANDSHAKE_CAPABILITIES),
                    },
                )
                return
            if url.path == "/api/v1/replica/identity":
                if self.command != "GET":
                    self._error(405, "method.not_allowed", "method not allowed")
                    return
                identity = self._authenticated_replica()
                if identity is None:
                    self._error(401, "auth.required", "valid bearer token required")
                    return
                self._send_json(200, {
                    "principalId": identity.principal_id,
                    "replicaId": identity.replica_id,
                })
                return
            if url.path == "/api/v1/replica/events":
                if self.command != "GET":
                    self._error(405, "method.not_allowed", "method not allowed")
                    return
                self._stream_events()
                return
            route = _route(url.path)
            if route is None:
                self._error(404, "route.not_found", "route not found")
                return
            if not self._authorized():
                self._error(401, "auth.required", "valid bearer token required")
                return
            log_id, action, record_id = route
            error = self._authorize(log_id, action, self.command)
            if error is not None:
                self._error(error[0], error[1], error[2])
                return
            if action == "head" and self.command == "GET":
                head = self.server.store.head(log_id)
                self._send_json(
                    200,
                    {"head": None if head is None else head.to_value()},
                )
                return
            if action == "has" and self.command == "HEAD":
                status = (
                    204
                    if self.server.store.has(Position(log_id, record_id))
                    else 404
                )
                self.send_response(status)
                self.end_headers()
                return
            if action == "records" and self.command == "GET":
                self._read(log_id, parse_qs(url.query))
                return
            if action == "records" and self.command == "POST":
                self._append(log_id)
                return
            self._error(405, "method.not_allowed", "method not allowed")
        except _ResponseSent:
            pass
        except (
            InvalidReplicaData,
            MissingDependency,
            UnknownPosition,
            ValueError,
        ) as error:
            self._error(400, "request.invalid", str(error))
        except ReplicaLogError as error:
            self._error(400, "replica.invalid", str(error))
        except Exception:
            logger.exception("replica request failed")
            self._error(500, "server.failure", "server failure")

    def _read(self, log_id: str, query: dict[str, list[str]]) -> None:
        if set(query) - {"after", "limit"}:
            raise InvalidReplicaData("unknown query parameter")
        after_values = query.get("after", [])
        limit_values = query.get("limit", ["100"])
        if len(after_values) > 1 or len(limit_values) != 1:
            raise InvalidReplicaData("query parameter is repeated")
        try:
            limit = int(limit_values[0])
        except ValueError as error:
            raise InvalidReplicaData("limit must be an integer") from error
        if not 1 <= limit <= MAX_PAGE_RECORDS:
            raise InvalidReplicaData(
                f"limit must be between 1 and {MAX_PAGE_RECORDS}"
            )
        after = (
            None
            if not after_values
            else Position(log_id, after_values[0])
        )
        self._send_json(
            200, self.server.store.read(log_id, after, limit).to_value()
        )

    def _append(self, log_id: str) -> None:
        value = self._read_json()
        if not isinstance(value, dict) or set(value) != {
            "expectedHead",
            "records",
        }:
            raise InvalidReplicaData("append body shape is invalid")
        expected_value = value["expectedHead"]
        expected = (
            None
            if expected_value is None
            else Position.from_value(expected_value, log_id=log_id)
        )
        records_value = value["records"]
        if not isinstance(records_value, list):
            raise InvalidReplicaData("records must be an array")
        if len(records_value) > MAX_PAGE_RECORDS:
            raise InvalidReplicaData("too many records")
        records = tuple(
            ReplicatedRecord.from_value(record, log_id=log_id)
            for record in records_value
        )
        result = self.server.append_and_broadcast(log_id, expected, records)
        self._send_json(
            409 if result.kind == "changed" else 200,
            result.to_value(),
        )

    def _read_json(self) -> object:
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            raise InvalidReplicaData("content-type must be application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise InvalidReplicaData("content-length is required")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise InvalidReplicaData("content-length is invalid") from error
        if length < 0:
            raise InvalidReplicaData("content-length is invalid")
        if length > MAX_BODY_BYTES:
            self._error(413, "request.too_large", "request body is too large")
            raise _ResponseSent()
        data = self.rfile.read(length)
        if len(data) != length:
            raise InvalidReplicaData("request body ended early")
        try:
            return json.loads(
                data.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidReplicaData("request body is invalid JSON") from error

    def _stream_events(self) -> None:
        if not self._authorized():
            self._error(401, "auth.required", "valid bearer token required")
            return
        identity = self._authenticated_replica()
        authorization = self.server.log_authorization
        if identity is not None and authorization is not None:
            observes = lambda log_id: authorization.access(identity, log_id) is not None
            has_access = authorization.has_access(identity)
        else:
            readable_logs = frozenset(log_id for log_id, access in self.server.bindings.items() if access == "read")
            observes = readable_logs.__contains__
            has_access = bool(readable_logs)
        if not has_access:
            self._error(403, "log.not_exposed", "no readable Logs available")
            return
        subscriber: queue.Queue[str] = queue.Queue(maxsize=64)
        with self.server._subscriber_lock:
            self.server._subscribers.append((subscriber, observes))
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = subscriber.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: ")
                self.wfile.write(event.encode("utf-8"))
                self.wfile.write(b"\n\n")
                self.wfile.flush()
        except (ConnectionError, BrokenPipeError, OSError):
            pass
        finally:
            with self.server._subscriber_lock:
                entry = next((item for item in self.server._subscribers if item[0] is subscriber), None)
                if entry is not None:
                    self.server._subscribers.remove(entry)

    def _authenticated_replica(self) -> AuthenticatedReplica | None:
        if self.server.authenticator is None:
            return None
        token = self._bearer_token() or self._cookie(SESSION_COOKIE)
        if token is None:
            return None
        return self.server.authenticator.authenticate(token)

    def _authorized(self) -> bool:
        if self.server.authenticator is not None:
            return self._authenticated_replica() is not None
        return self._admin_authorized()

    def _admin_authorized(self) -> bool:
        expected = f"Bearer {self.server.replica_token}"
        actual = self.headers.get("Authorization", "")
        if hmac.compare_digest(actual, expected):
            return True
        token = self._cookie(OPERATOR_SESSION_COOKIE)
        return (
            token is not None
            and self.server.operator_authenticator is not None
            and self.server.operator_authenticator(token)
        )

    def _bearer_token(self) -> str | None:
        actual = self.headers.get("Authorization", "")
        return actual[7:] if actual.startswith("Bearer ") and len(actual) > 7 else None

    def _cookie(self, name: str) -> str | None:
        source = self.headers.get("Cookie")
        if source is None:
            return None
        cookies = SimpleCookie()
        try:
            cookies.load(source)
        except CookieError:
            return None
        value = cookies.get(name)
        return None if value is None or not value.value else value.value

    def _authorize(
        self, log_id: str, action: str, method: str
    ) -> tuple[int, str, str] | None:
        identity = self._authenticated_replica()
        authorization = self.server.log_authorization
        if identity is not None and authorization is not None:
            access = authorization.access(identity, log_id)
            if permits_log(access, action, method):
                return None
            if access is None:
                return (403, "log.not_exposed", f"Log {log_id} is not exposed")
            return (403, "capability.denied", f"Log {log_id} does not permit this operation")
        access = self.server.bindings.get(log_id)
        if access is None:
            return (403, "log.not_exposed", f"Log {log_id} is not exposed")
        if method == "POST":
            if action != "records":
                return (405, "method.not_allowed", "method not allowed")
            if access != "append":
                return (403, "capability.denied", f"Log {log_id} does not accept appends")
            return None
        if method in ("GET", "HEAD"):
            if action == "records" and method == "GET":
                pass
            elif action in _READ_ACTIONS:
                pass
            else:
                return (405, "method.not_allowed", "method not allowed")
            if access == "append" and action in ("head", "has"):
                return None
            if access != "read":
                return (403, "capability.denied", f"Log {log_id} is not readable")
            return None
        return (405, "method.not_allowed", "method not allowed")

    def _send_json(
        self,
        status: int,
        value: object,
        headers: Iterable[tuple[str, str]] = (),
    ) -> None:
        body = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, header_value in headers:
            self.send_header(name, header_value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send_json(
            status, {"error": {"code": code, "message": message}}
        )

    def log_message(self, format: str, *args: object) -> None:
        logger.info("%s - %s", self.address_string(), format % args)


class _ResponseSent(Exception):
    pass


def _route(path: str) -> tuple[str, str, str] | None:
    parts = path.split("/")
    if len(parts) < 7 or parts[:5] != ["", "api", "v1", "replica", "logs"]:
        return None
    log_id = unquote(parts[5])
    if not log_id:
        return None
    if len(parts) == 7 and parts[6] in ("head", "records"):
        return log_id, parts[6], ""
    if len(parts) == 8 and parts[6] == "records":
        record_id = unquote(parts[7])
        return None if not record_id else (log_id, "has", record_id)
    return None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidReplicaData(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise InvalidReplicaData(f"invalid JSON number {value}")
