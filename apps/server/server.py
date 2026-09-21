"""PicoPG HTTP service using only the Python standard library.

Two things live here.  Reads hand a client the published course content it
asks for: the catalog, one commit's manifest and one file's exact bytes.
Writes accept a bundle from the operator and a bounded
submission from a client.  Everything a read needs is public, so the read path
holds no session, no role, and no per-caller decision; every write is bounded
and validated before it reaches the repository or the database.
"""

import hashlib
import json
import os
from contextlib import closing
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import events
import events_types
import manifests
import playgrounds as playgrounds_module
import pagination
import routes
import sync
import sync_handlers
from api_handler import ApiHandler, BASE_VERSION_HEADER, MAX_JSON_BODY_BYTES
from bundles import (
    MAX_ARCHIVE_BYTES,
    BundleValidationError,
    decode_bundle_zip,
    validate_bundle,
)
from database import immediate_transaction, open_database
from idempotency import (
    IdempotencyConflict,
    IdempotencyRequestError,
    execute as execute_idempotent,
)
from playgrounds import (
    InvalidRequestError,
    MissingPlaygroundError,
    PlaygroundConflictError,
    PlaygroundBaseChangedError,
    PlaygroundStoppedError,
    create_playground,
    get_playground,
    publish_playground,
    record_archive_event,
    restore_playground,
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
    backfill_submission_files as submissions_backfill,
    InvalidSubmissionRequestError,
    MissingSubmissionError,
    StoredSubmissionError,
    get_submission,
    list_submissions,
    submission_ordinal,
    submission_to_mapping,
    write_submission_files,
)


HOST = "127.0.0.1"
PORT = 8001
DATABASE_PATH = Path("data/app.db")

COURSE_FILE_CACHE_CONTROL = "public, max-age=31536000, immutable"

#: The readable copy of each handed-in work lives here, beside the database.
SUBMISSIONS_DIRECTORY_NAME = "submissions"


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


def _bundle_request(playground_id, bundle, base_version_id):
    """Return the canonical, content-addressed logical publish request.

    The identity is derived from the normalized validated bundle rather than
    the raw ZIP bytes, so repacking identical content with different
    compression or member order is the same logical request. The base is part
    of the identity too: the same bundle offered against a different Version is
    a different request, with a different answer.
    """

    return {
        "playgroundId": playground_id,
        "baseVersionId": base_version_id,
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


class Handler(ApiHandler):
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

    def _route_get(self, head_only=False):
        path = urlsplit(self.path).path

        if path == "/healthz":
            self._respond(200, "ok\n", head_only=head_only)
        elif path == routes.CATALOG_PATH:
            self._get_catalog(head_only=head_only)
        elif routes.match_manifest(path) is not None:
            self._get_manifest(*routes.match_manifest(path), head_only=head_only)
        elif routes.match_version_files(path) is not None:
            self._get_version_file(
                *routes.match_version_files(path), head_only=head_only
            )
        elif path == routes.SUBMISSIONS_PATH:
            self._get_submissions(head_only=head_only)
        elif routes.match_submission(path) is not None:
            self._get_submission(
                routes.match_submission(path), head_only=head_only
            )
        elif routes.allowed_methods(path) is not None:
            self._method_not_allowed(head_only=head_only)
        else:
            self._not_found(head_only=head_only)

    def _route_post(self):
        path = urlsplit(self.path).path

        if path == routes.PLAYGROUNDS_PATH:
            self._post_playground()
        elif routes.match_playground_versions(path) is not None:
            self._post_playground_versions(routes.match_playground_versions(path))
        elif routes.match_playground_restore(path) is not None:
            self._post_restore(routes.match_playground_restore(path))
        elif path == routes.SYNC_PATH:
            self._post_sync()
        elif routes.allowed_methods(path) is not None:
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

        # Only once the round has committed: the folder is a projection of a
        # stored record, never a second thing to keep consistent. A failure here
        # answers 500, and the client's retry of the same event repaints it.
        self._write_submitted_work(request, response)
        self._respond_json(200, json.loads(response.to_json()))

    def _write_submitted_work(self, request, response):
        """Write a folder per accepted hand-in, for a human to open.

        The row is the record and the folder is a copy of it, so this never
        runs inside the sync transaction: an aborted round must not leave a
        folder behind, and a folder must not be able to fail a stored hand-in.
        It runs on replays too, which is what makes a lost folder self-healing.
        """
        accepted = {
            result.event_id
            for result in response.event_results
            if result.status == "accepted"
        }
        pending = [
            envelope
            for envelope in request.pending_events
            if envelope.kind == events_types.EVENT_KIND_SUBMIT_CODE
            and envelope.event_id in accepted
        ]
        if not pending:
            return

        with self._database() as connection:
            for envelope in pending:
                stored = events.read_event(connection, envelope.event_id)
                if stored is None:
                    continue
                try:
                    body = json.loads(stored[3])
                    submission = body["submission"]
                except (TypeError, ValueError, KeyError):
                    # A stored response this build cannot read is not a reason to
                    # fail a round that already committed; the folder is a copy.
                    continue
                ordinal = submission_ordinal(
                    connection,
                    submission["student"],
                    submission["id"],
                    submission["created_at"],
                )
                write_submission_files(
                    self.server.submissions_path,
                    submission["id"],
                    submission["student"],
                    submission["playground_id"],
                    submission["version_id"],
                    submission["created_at"],
                    envelope.payload["files"],
                    ordinal,
                )

    def _route_delete(self):
        path = urlsplit(self.path).path
        playground_id = routes.match_playground(path)

        if playground_id is not None:
            self._delete_playground(playground_id)
        elif routes.allowed_methods(path) is not None:
            self._method_not_allowed()
        else:
            self._not_found()

    def _post_restore(self, playground_id):
        """Put an archived Playground back into the catalog.

        The counterpart of the `DELETE` on the same path. Clients learn about it
        the same way they learn about anything: the next sync carries a
        `publish_playground` entry for the Version it returns to.
        """
        if not self._require_operator():
            return

        try:
            with self._database() as connection:
                restored = restore_playground(connection, playground_id)
        except InvalidRequestError:
            self._api_error(400, "invalid_request")
            return
        except MissingPlaygroundError:
            self._not_found()
            return

        self._respond_json(
            200, {"status": "accepted" if restored else "no_op"}
        )

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
                    with immediate_transaction(connection):
                        # Drop the recorded publishes so an exact replay of an
                        # old idempotency key cannot report a 201 for a course
                        # nobody can read. The events table keeps the archive
                        # record, so a sync round still sees what happened.
                        connection.execute(
                            "DELETE FROM idempotency_results "
                            "WHERE scope = ? AND endpoint = ?",
                            (
                                OPERATOR_SCOPE,
                                routes.playground_versions_endpoint(playground_id),
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
        already pinned to it. A row whose stored identifier is not representable
        fails closed.

        Everything comes from the database alone. Resolving each row's ref with
        Git would be one subprocess per Playground per request — measured at 2.2
        seconds for 300 courses, paid on every launch and every refresh — and
        ``current_version_id`` is stored precisely so that is not necessary. A
        row that has never had one is left out; start-up fills those in
        (`playgrounds.backfill_current_versions`).
        """
        rows = connection.execute(
            "SELECT id, current_version_id FROM playgrounds WHERE state = 'active' "
            "ORDER BY created_at, id"
        ).fetchall()

        items = []
        for row in rows:
            if not valid_playground_id(row["id"]):
                raise RepositoryError("stored playground identifier is invalid")
            version_id = row["current_version_id"]
            if not version_id:
                # Never written: no publish has ever stored one. It is not in
                # the catalog rather than resolved here, so a request never
                # pays for another row's missing value.
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

        # The base names the Version this bundle was built on; an empty header
        # means "the first one", and `publish_playground` decides whether that
        # is true. A header that is present but blank is the same as absent.
        base_version_id = self.headers.get(BASE_VERSION_HEADER, "").strip() or None

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
                    routes.playground_versions_endpoint(playground_id),
                    request_key,
                    _bundle_request(playground_id, bundle, base_version_id),
                    lambda connection: self._publish_bundle(
                        connection, playground_id, bundle, base_version_id
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
        except PlaygroundBaseChangedError:
            # protocol.md's `conflict`: the base is stale. Nothing was written,
            # and the publisher resolves it by reading the current Version.
            self._respond_json(409, {"status": "conflict"})
            return
        except PlaygroundHeadChangedError:
            # The ref moved under us: a race with another publish, with the same
            # answer and the same remedy.
            self._respond_json(409, {"status": "conflict"})
            return
        except RepositoryError:
            self._api_error(500, "internal_error")
            return

        self._respond_json(status, body)

    def _publish_bundle(self, connection, playground_id, bundle, base_version_id):
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
            base_version_id,
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

    @staticmethod
    def _playground_payload(playground):
        return {
            "id": playground.id,
            "createdAt": playground.created_at,
        }

    def _method_not_allowed(self, head_only=False):
        path = urlsplit(self.path).path
        methods = routes.allowed_methods(path)
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

    # Route-aware method boundary for unsupported verbs.
    def do_OPTIONS(self):
        self._dispatch(self._method_not_allowed)


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
    submissions_path=None,
    operator_token=None,
):
    """Return an initialized HTTP server bound to ``host:port``.

    ``submissions_path`` is where the readable copy of each handed-in work goes.
    It defaults to a sibling of the database, so every deployment path stays
    under one data directory; the directory is created lazily by the writer, so
    a read-only or unused one never becomes a start-up failure.
    """
    operator_token = _validate_operator_token(operator_token)

    database_path = Path(database_path)
    database = open_database(database_path)
    database.close()

    if submissions_path is None:
        submissions_path = database_path.parent / SUBMISSIONS_DIRECTORY_NAME
    else:
        submissions_path = Path(submissions_path)

    repository_path = Path(repository_path)
    repository = open_repository(repository_path)

    # Rows written before `current_version_id` existed have no value, and the
    # catalog no longer resolves one on demand. Fill them in once, here, so a
    # request never pays a Git subprocess per row.
    with closing(open_database(database_path)) as connection:
        # The repository's refs are brought into agreement with the database
        # before the first request: the history refs move into their namespace,
        # then rows that never stored a version get one.
        playgrounds_module.migrate_legacy_refs(connection, repository)
        playgrounds_module.backfill_current_versions(
            connection,
            lambda playground_id: manifests.current_playground_commit(
                repository, playground_id
            ),
        )
        # Same idea for the readable copy of handed-in work: the row is the
        # record, so a missing folder is written rather than reported.
        submissions_backfill(connection, submissions_path)

    server = SlimHTTPServer((host, port), Handler)
    server.database_path = database_path
    server.repository_path = repository_path
    server.repository = repository
    server.submissions_path = submissions_path
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
