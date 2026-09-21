"""PicoPG HTTP service using only the Python standard library.

Two things live here.  Reads hand a client the published course content it
asks for: the catalog, one commit's manifest and one file's exact bytes.
Writes accept a bundle from the operator and a bounded
submission from a client.  Everything a read needs is public, so the read path
holds no session, no role, and no per-caller decision; every write is bounded
and validated before it reaches the repository or the database.
"""

import hashlib
import hmac
import json
import os
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import events
import events_types
import manifests
import pagination
import sync
import sync_handlers
from bundles import (
    MAX_ARCHIVE_BYTES,
    BundleValidationError,
    decode_bundle_zip,
    validate_bundle,
)
from database import open_database
from idempotency import (
    MAX_REQUEST_KEY_LENGTH,
    IdempotencyConflict,
    IdempotencyRequestError,
    execute as execute_idempotent,
)
from playgrounds import (
    InvalidRequestError,
    MissingPlaygroundError,
    PlaygroundConflictError,
    PlaygroundStoppedError,
    create_playground,
    get_playground,
    publish_playground,
    record_archive_event,
)
from repository import (
    DEFAULT_REPOSITORY_PATH,
    CommitMetadata,
    PlaygroundHeadChangedError,
    RepositoryError,
    open_repository,
    valid_playground_id,
)
from submissions import (
    InvalidSubmissionRequestError,
    MissingSubmissionError,
    StoredSubmissionError,
    get_submission,
    list_submissions,
    submission_to_mapping,
)


HOST = "127.0.0.1"
PORT = 8001
DATABASE_PATH = Path("data/app.db")

MAX_JSON_BODY_BYTES = 16 * 1024

COURSE_FILE_CACHE_CONTROL = "public, max-age=31536000, immutable"

ALLOWED_METHODS = ("GET", "HEAD")
PLAYGROUNDS_PATH = "/api/v1/playgrounds"
PLAYGROUND_VERSIONS_SUFFIX = "/versions"
VERSIONS_SEGMENT = "versions"
FILES_SEGMENT = "files"
MANIFEST_SEGMENT = "manifest"
CATALOG_PATH = "/api/v1/catalog"
SUBMISSIONS_PATH = "/api/v1/submissions"
SYNC_PATH = "/api/v1/sync"

# One sync round may carry at most this many pending events. The body is
# already bounded, so this only keeps a single request's per-event work (and
# its response) proportional to what a client can legitimately have queued
# offline.
MAX_PENDING_EVENTS = 100

# The idempotency scope of the single operator that publishes bundles, so the
# recorded result of a publish can never collide with anything else.
OPERATOR_SCOPE = "operator"

# The operator token is the whole write authorization: publishing a bundle and
# reading what was handed in.  It is supplied at start-up, compared in constant
# time, and never stored in the database, so this service holds no account,
# no session, and no password.
OPERATOR_ENVIRONMENT_VARIABLE = "PICOPG_OPERATOR_TOKEN"
MIN_OPERATOR_TOKEN_BYTES = 16


def _match_playground_versions(path):
    """Return the playground ID in a commit upload path, else None."""
    prefix = PLAYGROUNDS_PATH + "/"
    if not path.startswith(prefix) or not path.endswith(
        PLAYGROUND_VERSIONS_SUFFIX
    ):
        return None

    playground_id = path[len(prefix): -len(PLAYGROUND_VERSIONS_SUFFIX)]
    if not playground_id or "/" in playground_id:
        return None
    return playground_id


def _match_playground(path):
    """Return the playground ID in ``/api/v1/playgrounds/<id>``, else None."""

    prefix = PLAYGROUNDS_PATH + "/"
    if not path.startswith(prefix):
        return None

    playground_id = path[len(prefix):]
    if not playground_id or "/" in playground_id:
        return None
    return playground_id


def _playground_versions_endpoint(playground_id):
    """The idempotency endpoint of a Playground's bundle publish.

    One spelling, used by both the publish that records a result and the retire
    that has to forget it, so the two can never drift apart.
    """

    return f"POST {PLAYGROUNDS_PATH}/{playground_id}{PLAYGROUND_VERSIONS_SUFFIX}"


def _version_route_parts(path):
    """Split ``.../<playground>/versions/<version>/<tail>`` for read routes.

    Returns ``(playground_id, version_id, tail)`` where ``tail`` is
    everything after ``<version>/`` and may still contain literal slashes.
    Returns ``None`` for any other shape, so a missing or extra segment never
    matches. ``version_id`` is a Git commit id in this implementation; the
    protocol only promises it addresses a Version.
    """

    prefix = PLAYGROUNDS_PATH + "/"
    if not path.startswith(prefix):
        return None

    parts = path[len(prefix):].split("/", 3)
    if len(parts) != 4:
        return None
    playground_id, segment, version_id, tail = parts
    if segment != VERSIONS_SEGMENT or not playground_id or not version_id:
        return None
    return playground_id, version_id, tail


def _match_manifest(path):
    """Return ``(playground_id, version_id)`` for a manifest read path."""

    parts = _version_route_parts(path)
    if parts is None:
        return None
    playground_id, version_id, tail = parts
    if tail != MANIFEST_SEGMENT:
        return None
    return playground_id, version_id


def _match_version_files(path):
    """Return ``(playground_id, version_id, encoded_path)`` for a file path."""

    parts = _version_route_parts(path)
    if parts is None:
        return None
    playground_id, version_id, tail = parts
    prefix = FILES_SEGMENT + "/"
    if not tail.startswith(prefix):
        return None
    encoded_path = tail[len(prefix):]
    if not encoded_path:
        return None
    return playground_id, version_id, encoded_path


def _match_submission(path):
    """Return the opaque submission ID in a submission path, else None."""
    prefix = SUBMISSIONS_PATH + "/"
    if not path.startswith(prefix):
        return None

    submission_id = path[len(prefix):]
    if not submission_id or "/" in submission_id:
        return None
    return submission_id


def _allowed_methods(path):
    """Return the methods a known path accepts, or ``None`` when unknown."""
    if path in ("/healthz", CATALOG_PATH):
        return ALLOWED_METHODS
    if path == SUBMISSIONS_PATH:
        return ALLOWED_METHODS
    if path == SYNC_PATH:
        return ("POST",)
    if path == PLAYGROUNDS_PATH:
        return ("POST",)
    if _match_playground(path) is not None:
        return ("DELETE",)
    if _match_playground_versions(path) is not None:
        return ("POST",)
    if _match_manifest(path) is not None:
        return ALLOWED_METHODS
    if _match_version_files(path) is not None:
        return ALLOWED_METHODS
    if _match_submission(path) is not None:
        return ALLOWED_METHODS
    return None


def _decode_sync_request(payload):
    """Parse and validate a ``SyncRequest`` body.

    Returns the request, or ``None`` when the body is malformed. Unknown
    *fields* are ignored, as protocol.md requires; an unknown event *kind* is
    not malformed, so it is left for the engine to reject per event.
    """

    if type(payload) is not dict:
        return None
    pending = payload.get("pending_events")
    after_seq = payload.get("after_seq")
    if type(pending) is not list:
        return None
    if type(after_seq) is not int or after_seq < 0:
        return None
    if len(pending) > MAX_PENDING_EVENTS:
        return None

    envelopes = []
    for entry in pending:
        try:
            envelopes.append(events_types.EventEnvelope.from_mapping(entry))
        except ValueError:
            return None
    return events_types.SyncRequest(
        pending_events=tuple(envelopes), after_seq=after_seq
    )


def _bundle_request(playground_id, bundle):
    """Return the canonical, content-addressed logical publish request.

    The identity is derived from the normalized validated bundle rather than
    the raw ZIP bytes, so repacking identical content with different
    compression or member order is the same logical request.
    """

    return {
        "playgroundId": playground_id,
        "files": [
            {
                "path": path,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for path, data in bundle.items()
        ],
    }


def _decode_content_path(encoded_path):
    """Decode one URL path exactly once, rejecting encoded separators.

    Encoded slashes and backslashes are rejected instead of being decoded into
    ambiguous path separators; ``%2e%2e`` and similar traversals are left for
    the bundle path rules to reject after the single decode.
    """

    lowered = encoded_path.lower()
    if "%2f" in lowered or "%5c" in lowered:
        return None
    try:
        return unquote(encoded_path, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def _commit_metadata():
    """Build the fixed, server-owned identity every published commit carries.

    The publisher is not an account and is never taken from a request body, so
    a published commit cannot claim to be authored by anyone else.
    """

    return CommitMetadata(
        name="PicoPG Operator",
        email="operator@picopg.invalid",
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        message="Publish playground bundle",
    )


class Handler(BaseHTTPRequestHandler):
    """Route the public reads and the writes."""

    def do_GET(self):
        self._dispatch(self._route_get)

    def do_HEAD(self):
        self._dispatch(self._route_get, head_only=True)

    def do_POST(self):
        self._dispatch(self._route_post)

    # The service has no PUT or PATCH.  Implementing them as the shared
    # method-not-allowed answer keeps one 405 vocabulary instead of the
    # standard library's 501 HTML page.
    def do_PUT(self):
        self._dispatch(self._method_not_allowed)

    def do_PATCH(self):
        self._dispatch(self._method_not_allowed)

    def do_DELETE(self):
        self._dispatch(self._route_delete)

    def _dispatch(self, route, **kwargs):
        self._headers_sent = False
        try:
            route(**kwargs)
        except Exception:
            self._handle_unexpected_failure()

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

    def _route_get(self, head_only=False):
        path = urlsplit(self.path).path

        if path == "/healthz":
            self._respond(200, "ok\n", head_only=head_only)
        elif path == CATALOG_PATH:
            self._get_catalog(head_only=head_only)
        elif _match_manifest(path) is not None:
            self._get_manifest(*_match_manifest(path), head_only=head_only)
        elif _match_version_files(path) is not None:
            self._get_version_file(
                *_match_version_files(path), head_only=head_only
            )
        elif path == SUBMISSIONS_PATH:
            self._get_submissions(head_only=head_only)
        elif _match_submission(path) is not None:
            self._get_submission(
                _match_submission(path), head_only=head_only
            )
        elif _allowed_methods(path) is not None:
            self._method_not_allowed(head_only=head_only)
        else:
            self._not_found(head_only=head_only)

    def _route_post(self):
        path = urlsplit(self.path).path

        if path == PLAYGROUNDS_PATH:
            self._post_playground()
        elif _match_playground_versions(path) is not None:
            self._post_playground_versions(_match_playground_versions(path))
        elif path == SYNC_PATH:
            self._post_sync()
        elif _allowed_methods(path) is not None:
            self._method_not_allowed()
        else:
            self._not_found()

    def _post_sync(self):
        """Process one ``SyncRequest`` and return a ``SyncResponse``.

        Pending events are matched against the events table by ``event_id``
        (a replay returns the stored result) and handed to the per-kind
        handler otherwise. The response carries those results, every content
        log entry the client has not yet seen, the new cursor and the two
        protocol-negotiation fields.
        """
        payload = self._read_json(max_bytes=MAX_JSON_BODY_BYTES)
        if payload is None:
            return

        request = _decode_sync_request(payload)
        if request is None:
            self._api_error(400, "invalid_request")
            return

        repository = self.server.repository
        try:
            with self._database() as connection:
                response = sync.process_sync(
                    connection,
                    request,
                    submit_handler=lambda connection, envelope: sync_handlers.submit_handler(
                        connection, envelope, repository=repository
                    ),
                    publish_handler=sync_handlers.publish_handler,
                    archive_handler=sync_handlers.archive_handler,
                )
        except ValueError:
            self._api_error(400, "invalid_request")
            return

        self._respond_json(200, json.loads(response.to_json()))

    def _route_delete(self):
        path = urlsplit(self.path).path
        playground_id = _match_playground(path)

        if playground_id is not None:
            self._delete_playground(playground_id)
        elif _allowed_methods(path) is not None:
            self._method_not_allowed()
        else:
            self._not_found()

    def _delete_playground(self, playground_id):
        """Take a Playground out of the catalog.

        Students stop seeing it on their next refresh; nothing else changes.
        The database row stays (submissions keep a valid parent) and the
        commits stay in the repository, so publishing the same id again
        restores it. The playground is marked ``state = stopped`` and an
        ``archive_playground`` event is emitted so clients learn about the
        change through their next sync; the Git ref is left intact so
        history remains reachable.

        Re-archiving a stopped playground is a no-op: the existing event
        is replayed by the events table, the database row keeps the same
        state and the response carries the same status.
        """

        if not self._require_operator():
            return

        try:
            with self._database() as connection:
                stored = get_playground(connection, playground_id)
                if stored is None:
                    self._not_found()
                    return
                already_stopped = stored.state == "stopped"
                if not already_stopped:
                    # One transaction: the archive event, its content log row
                    # and the state flip all land together, or none of them do.
                    with connection:
                        # Drop the recorded publishes so an exact replay of an
                        # old idempotency key cannot report a 201 for a course
                        # nobody can read. The events table keeps the archive
                        # record, so a sync round still sees what happened.
                        connection.execute(
                            "DELETE FROM idempotency_results "
                            "WHERE scope = ? AND endpoint = ?",
                            (
                                OPERATOR_SCOPE,
                                _playground_versions_endpoint(playground_id),
                            ),
                        )
                        record_archive_event(connection, playground_id)
        except InvalidRequestError:
            self._api_error(400, "invalid_request")
            return

        # protocol.md's ArchiveResult: the status alone says whether this call
        # changed anything.
        self._respond_json(
            200, {"status": "no_op" if already_stopped else "accepted"}
        )

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

    def _get_catalog(self, head_only=False):
        """Return the current snapshot: the cursor plus every active version.

        protocol.md's ``Catalog`` is a whole snapshot, not a paged list: a new
        client installs exactly this set and adopts ``upto_seq`` as its
        cursor, then follows the content log through sync. The Playgrounds
        are ordered by the order they were first published, so a client that
        re-reads the catalog sees a stable list.
        """
        try:
            with self._database() as connection:
                playgrounds = self._active_playgrounds(connection)
                upto_seq = events.current_upto_seq(connection)
        except RepositoryError:
            self._api_error(500, "internal_error", head_only=head_only)
            return

        self._respond_json(
            200,
            {
                "upto_seq": upto_seq,
                "protocol_version": events_types.PROTOCOL_VERSION,
                "minimum_client_protocol_version": (
                    events_types.MINIMUM_CLIENT_PROTOCOL_VERSION
                ),
                "playgrounds": playgrounds,
            },
            head_only=head_only,
        )

    def _active_playgrounds(self, connection):
        """Every ``{playground_id, current_version_id}`` a client may install.

        Archived Playgrounds are filtered out by ``state`` rather than by the
        absence of a Git ref, so their history stays readable for a client
        already pinned to it. A row whose ref cannot be resolved is skipped
        instead of failing the whole catalog; a row whose stored identifier
        is not representable fails closed.
        """
        rows = connection.execute(
            "SELECT id FROM playgrounds WHERE state = 'active' "
            "ORDER BY created_at, id"
        ).fetchall()

        items = []
        for row in rows:
            if not valid_playground_id(row["id"]):
                raise RepositoryError("stored playground identifier is invalid")
            version_id = manifests.current_playground_commit(
                self.server.repository, row["id"]
            )
            if version_id is None:
                continue
            items.append(
                {"playground_id": row["id"], "current_version_id": version_id}
            )
        return items

    def _get_manifest(self, playground_id, version_id, head_only=False):
        try:
            manifest = manifests.generate_manifest(
                self.server.repository, playground_id, version_id
            )
        except manifests.ManifestError:
            self._not_found(head_only=head_only)
            return
        except RepositoryError:
            self._api_error(500, "internal_error", head_only=head_only)
            return

        self._respond_json(
            200, manifests.manifest_to_mapping(manifest), head_only=head_only
        )

    def _get_version_file(
        self, playground_id, version_id, encoded_path, head_only=False
    ):
        path = _decode_content_path(encoded_path)
        if path is None:
            self._not_found(head_only=head_only)
            return

        try:
            content = manifests.read_commit_file(
                self.server.repository, playground_id, version_id, path
            )
        except manifests.ManifestError:
            self._not_found(head_only=head_only)
            return
        except RepositoryError:
            self._api_error(500, "internal_error", head_only=head_only)
            return

        # The client verifies every download against the manifest's sha256;
        # sending its own digest lets it refuse a body that changed in
        # transit without hashing against a value it took from the same
        # response.
        self._respond_bytes(
            200,
            content.data,
            content_type="application/octet-stream",
            etag=content.sha256,
            extra_headers=(("X-Content-SHA256", content.sha256),),
            cache_control=COURSE_FILE_CACHE_CONTROL,
            head_only=head_only,
        )

    def _post_playground(self):
        if not self._require_operator():
            return

        payload = self._read_json()
        if payload is None:
            return

        try:
            with self._database() as connection:
                playground = create_playground(connection, payload["id"])
        except KeyError:
            self._api_error(400, "invalid_request")
            return
        except InvalidRequestError:
            self._api_error(400, "invalid_request")
            return
        except PlaygroundConflictError:
            self._api_error(409, "conflict")
            return

        self._respond_json(
            201,
            {"playground": self._playground_payload(playground)},
        )

    def _post_playground_versions(self, playground_id):
        if not self._require_operator():
            return

        if self.headers.get_content_type() != "application/zip":
            self._api_error(415, "unsupported_media_type")
            return

        request_key = self._idempotency_key()
        if request_key is None:
            return

        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._api_error(400, "invalid_request")
            return

        if content_length < 0:
            self._api_error(400, "invalid_request")
            return
        if content_length > MAX_ARCHIVE_BYTES:
            self._api_error(413, "request_too_large")
            return

        archive = self.rfile.read(content_length)
        try:
            bundle = validate_bundle(decode_bundle_zip(archive))
        except BundleValidationError as error:
            self._api_error(
                400,
                "invalid_bundle",
                details={"reason": error.reason},
            )
            return

        try:
            with self._database() as connection:
                status, body = execute_idempotent(
                    connection,
                    OPERATOR_SCOPE,
                    _playground_versions_endpoint(playground_id),
                    request_key,
                    _bundle_request(playground_id, bundle),
                    lambda connection: self._publish_bundle(
                        connection, playground_id, bundle
                    ),
                )
        except IdempotencyConflict:
            self._api_error(409, "idempotency_conflict")
            return
        except IdempotencyRequestError:
            self._api_error(400, "invalid_request")
            return
        except MissingPlaygroundError:
            self._api_error(404, "not_found")
            return
        except InvalidRequestError:
            self._api_error(400, "invalid_request")
            return
        except PlaygroundStoppedError:
            # protocol.md's PublishResult: the Playground is not writable, and
            # retrying the same request will not change that.
            self._respond_json(409, {"status": "playground_stopped"})
            return
        except PlaygroundHeadChangedError:
            # The ref moved under us: the publisher's base is stale.
            self._respond_json(409, {"status": "conflict"})
            return
        except RepositoryError:
            self._api_error(500, "internal_error")
            return

        self._respond_json(status, body)

    def _publish_bundle(self, connection, playground_id, bundle):
        """Create a Version for one validated bundle.

        The result is protocol.md's ``PublishResult``: the protocol's own field
        names carry the outcome, so the publisher never has to read an HTTP
        status to know what happened.
        """
        existing = get_playground(connection, playground_id)
        if existing is None:
            raise MissingPlaygroundError("playground does not exist")
        result = publish_playground(
            connection,
            playground_id,
            self.server.repository,
            bundle,
            _commit_metadata(),
        )
        return 201, {
            "status": "no_op" if result.no_op else "accepted",
            "version_id": result.commit_id,
        }

    def _get_submissions(self, head_only=False):
        if not self._require_operator(head_only=head_only):
            return

        parsed = self._list_query(pagination.SUBMISSIONS_ROUTE, head_only)
        if parsed is None:
            return
        limit, after = parsed

        try:
            with self._database() as connection:
                page = list_submissions(connection, limit=limit, after=after)
        except (InvalidSubmissionRequestError, StoredSubmissionError):
            self._api_error(500, "internal_error", head_only=head_only)
            return

        self._respond_json(
            200,
            {
                "submissions": [
                    submission_to_mapping(item) for item in page.items
                ],
                "nextCursor": self._next_cursor(
                    pagination.SUBMISSIONS_ROUTE, page.next_key
                ),
            },
            head_only=head_only,
        )

    def _get_submission(self, submission_id, head_only=False):
        if not self._require_operator(head_only=head_only):
            return

        try:
            with self._database() as connection:
                stored = get_submission(connection, submission_id)
        except MissingSubmissionError:
            self._api_error(404, "not_found", head_only=head_only)
            return
        except StoredSubmissionError:
            self._api_error(500, "internal_error", head_only=head_only)
            return

        self._respond_json(
            200,
            {
                "submission": submission_to_mapping(
                    stored.submission, files=stored.files
                )
            },
            head_only=head_only,
        )

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

    @staticmethod
    def _playground_payload(playground):
        return {
            "id": playground.id,
            "createdAt": playground.created_at,
        }

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

    def _method_not_allowed(self, head_only=False):
        path = urlsplit(self.path).path
        methods = _allowed_methods(path)
        if methods is None:
            self._not_found(head_only=head_only)
            return

        headers = (("Allow", ", ".join(methods)),)
        if path.startswith("/api/"):
            self._api_error(
                405,
                "method_not_allowed",
                head_only=head_only,
                extra_headers=headers,
            )
        else:
            self._respond(
                405,
                "method not allowed\n",
                head_only=head_only,
                extra_headers=headers,
            )

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

    # Route-aware method boundary for unsupported verbs.
    def do_OPTIONS(self):
        self._dispatch(self._method_not_allowed)

    def log_message(self, format, *args):
        """Suppress the default access log line.

        ``BaseHTTPRequestHandler`` writes the raw request target (including
        any query string) to stderr for every request.  Nothing here needs it.
        """
        return


class SlimHTTPServer(ThreadingHTTPServer):
    """Serve requests without leaking a traceback to the operator.

    ``socketserver.BaseServer.handle_error`` prints a full traceback to
    ``sys.stderr``, file paths and all, for any request that aborts after the
    response started.  An aborted connection is the only outcome left at that
    point, so the traceback is dropped.
    """

    def handle_error(self, request, client_address):
        return


def _validate_operator_token(token):
    """Return the operator token, refusing to start without a strong one.

    There is no account database to fall back on, so a missing or guessable
    token would make every write endpoint public.  Failing at start-up is the
    only safe answer.
    """

    if token is None:
        token = os.environ.get(OPERATOR_ENVIRONMENT_VARIABLE)
    if type(token) is not str or len(token.encode("utf-8")) < MIN_OPERATOR_TOKEN_BYTES:
        raise ValueError(
            f"{OPERATOR_ENVIRONMENT_VARIABLE} must be set to at least "
            f"{MIN_OPERATOR_TOKEN_BYTES} bytes"
        )
    return token


def make_server(
    host=HOST,
    port=PORT,
    database_path=DATABASE_PATH,
    repository_path=DEFAULT_REPOSITORY_PATH,
    operator_token=None,
):
    """Return an initialized HTTP server bound to ``host:port``."""
    operator_token = _validate_operator_token(operator_token)

    database_path = Path(database_path)
    database = open_database(database_path)
    database.close()

    repository_path = Path(repository_path)
    repository = open_repository(repository_path)

    server = SlimHTTPServer((host, port), Handler)
    server.database_path = database_path
    server.repository_path = repository_path
    server.repository = repository
    server.operator_token = operator_token
    return server


def main():
    server = make_server()
    host, port = server.server_address[:2]
    print(f"Pico service: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
