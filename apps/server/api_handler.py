"""The HTTP plumbing every endpoint sits on.

This is the layer between `BaseHTTPRequestHandler` and the service: reading a
bounded JSON body, writing a bounded JSON or byte response, deciding what a
non-2xx means, refusing a request whose protocol version this server cannot
serve, holding the operator token, and keeping every request on its own database
connection.

It knows nothing about Playgrounds, bundles, Git or submissions — those live in
`server.py` and the domain modules — so everything here can be understood from
its shape alone, and an endpoint cannot reach storage through it by accident.
"""

import hmac
import json
from contextlib import closing
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

import events_types
import pagination
from database import open_database
from idempotency import MAX_REQUEST_KEY_LENGTH

#: A JSON request body (a publish sends a ZIP; a submission body is bounded
#: larger in `server.py`); anything bigger is refused before it is read.
MAX_JSON_BODY_BYTES = 16 * 1024

# protocol.md §4: every request states which protocol the client speaks. It
# cannot travel in a payload — a catalog request has no parameters and a file
# read is a bare GET — so it is a header, checked once before routing.
PROTOCOL_VERSION_HEADER = "X-Protocol-Version"


class ApiHandler(BaseHTTPRequestHandler):
    """One request, from its headers to its response, with no domain knowledge.

    A subclass supplies the routes (``_route_get``, ``_route_post``, …) and the
    handlers those routes name. This class supplies the envelope around them:
    the protocol-version gate, the unexpected-failure guard, one database
    connection per request, and the response shapes every endpoint shares.
    """

    def _dispatch(self, route, **kwargs):
        self._headers_sent = False
        if not self._accepts_client_protocol(**kwargs):
            return
        try:
            route(**kwargs)
        except Exception:
            self._handle_unexpected_failure()

    def _accepts_client_protocol(self, head_only=False):
        """Refuse a request whose protocol version this server cannot serve.

        A request that does not say which protocol it speaks cannot be served
        safely — the answer would be interpreted under an unknown contract —
        so it is a malformed request. A version below the oldest this server
        still speaks gets ``426``, which is a refusal the client can turn into
        "upgrade me" instead of a silently wrong result.

        ``/healthz`` is exempt: a probe is not a client and has no protocol.
        """
        if not urlsplit(self.path).path.startswith("/api/v1/"):
            return True

        raw = self.headers.get(PROTOCOL_VERSION_HEADER)
        version = raw.strip() if raw is not None else ""
        if not version.isascii() or not version.isdigit():
            self._api_error(400, "invalid_request", head_only=head_only)
            return False
        if int(version) < events_types.MINIMUM_CLIENT_PROTOCOL_VERSION:
            self._api_error(426, "client_too_old", head_only=head_only)
            return False
        return True

    def _handle_unexpected_failure(self):
        """Turn an unexpected route exception into one bounded response.

        Once response headers are committed no second response is possible,
        so the connection is aborted instead.
        """

        if self._headers_sent:
            self.close_connection = True
            return
        try:
            self._api_error(500, "internal_error")
        except Exception:
            self.close_connection = True

    def _respond_bytes(
        self,
        status,
        data,
        *,
        content_type,
        etag=None,
        extra_headers=(),
        cache_control="no-store",
        head_only=False,
    ):
        if etag is not None and self._if_none_match(etag):
            self._respond_not_modified(head_only=head_only)
            return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if etag is not None:
            self.send_header("ETag", f'"{etag}"')
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self._headers_sent = True
        if not head_only:
            self.wfile.write(data)

    def _if_none_match(self, etag):
        header = self.headers.get("If-None-Match")
        return header is not None and header.strip() == f'"{etag}"'

    def _respond_not_modified(self, head_only=False):
        self.send_response(304)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._headers_sent = True

    def _read_json(self, max_bytes=MAX_JSON_BODY_BYTES):
        if self.headers.get_content_type() != "application/json":
            self._api_error(415, "unsupported_media_type")
            return None

        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._api_error(400, "invalid_request")
            return None

        if content_length < 0:
            self._api_error(400, "invalid_request")
            return None
        if content_length > max_bytes:
            self._api_error(413, "request_too_large")
            return None

        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._api_error(400, "invalid_json")
            return None

        if not isinstance(payload, dict):
            self._api_error(400, "invalid_request")
            return None
        return payload

    def _bearer_token(self):
        scheme, separator, token = self.headers.get("Authorization", "").partition(" ")
        if separator and scheme.lower() == "bearer" and token and " " not in token:
            return token
        return None

    def _idempotency_key(self):
        """Return the request's idempotency key, or answer 400."""

        request_key = self.headers.get("Idempotency-Key")
        if request_key is None or not request_key.strip():
            self._api_error(400, "invalid_request")
            return None
        if len(request_key) > MAX_REQUEST_KEY_LENGTH:
            self._api_error(400, "invalid_request")
            return None
        return request_key

    def _require_operator(self, head_only=False):
        """Return whether the request carries the operator token.

        The token is the only write authorization left.  It is compared in
        constant time against the value supplied at start-up, and a missing or
        wrong token is answered with one opaque 401 so a caller cannot tell a
        missing token from a wrong one.
        """

        supplied = self._bearer_token()
        expected = self.server.operator_token
        if supplied is None or not hmac.compare_digest(supplied, expected):
            self._api_error(401, "invalid_credentials", head_only=head_only)
            return False
        return True

    def _database(self):
        return closing(open_database(self.server.database_path))

    def _list_query(self, route, head_only=False):
        """Parse the common ``limit``/``cursor`` query or answer 400."""

        try:
            return pagination.parse_pagination(
                urlsplit(self.path).query, route
            )
        except pagination.PaginationError:
            self._api_error(400, "invalid_request", head_only=head_only)
            return None

    @staticmethod
    def _next_cursor(route, next_key):
        if next_key is None:
            return None
        return pagination.encode_cursor(route, next_key)

    def _not_found(self, head_only=False):
        if urlsplit(self.path).path.startswith("/api/"):
            self._api_error(404, "not_found", head_only=head_only)
        else:
            self._respond(404, "not found\n", head_only=head_only)

    def _api_error(
        self, status, code, head_only=False, extra_headers=(), details=None
    ):
        payload = {"code": code}
        if details is not None:
            payload["details"] = details
        self._respond_json(
            status,
            payload,
            head_only=head_only,
            extra_headers=extra_headers,
        )

    def _respond_json(self, status, payload, head_only=False, extra_headers=()):
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._respond(
            status,
            text,
            head_only=head_only,
            extra_headers=extra_headers,
            content_type="application/json; charset=utf-8",
        )

    def _respond(
        self,
        status,
        text,
        head_only=False,
        extra_headers=(),
        content_type="text/plain; charset=utf-8",
    ):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self._headers_sent = True
        if not head_only:
            self.wfile.write(body)

    def log_message(self, format, *args):
        """Suppress the default access log line.

        ``BaseHTTPRequestHandler`` writes the raw request target (including
        any query string) to stderr for every request.  Nothing here needs it.
        """
        return
