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

import assignments as assignments_module
import manifests
import playgrounds as playgrounds_module
import pagination
import routes
import uuid
from api_handler import (
    MAX_JSON_BODY_BYTES,
    MINIMUM_CLIENT_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    ApiHandler,
    BASE_VERSION_HEADER,
)
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
    lookup as idempotency_lookup,
)
from playgrounds import (
    InvalidRequestError,
    MissingPlaygroundError,
    PlaygroundBaseChangedError,
    PlaygroundConflictError,
    PlaygroundStoppedError,
    commit_prepared_version,
    create_playground,
    get_playground,
    observed_version,
    recover_pending_refs,
    require_publishable,
    restore_playground,
)
from repository import (
    DEFAULT_REPOSITORY_PATH,
    CommitMetadata,
    RepositoryError,
    commit_in_assignment_history,
    commit_in_playground_history,
    converge_assignment_ref,
    converge_history_ref,
    delete_pending_ref,
    open_repository,
    prepare_assignment_version,
    publish_playground_prepare,
    valid_playground_id,
)
from submissions import (
    backfill_submission_files as submissions_backfill,
    create_if_absent as create_submission,
    decode_delivery as decode_submission_delivery,
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


# The idempotency scope of the single operator that publishes bundles, so the
# recorded result of a publish can never collide with anything else.
OPERATOR_SCOPE = "operator"

# The operator token is the whole write authorization: publishing a bundle and
# reading what was handed in.  It is supplied at start-up, compared in constant
# time, and never stored in the database, so this service holds no account,
# no session, and no password.
OPERATOR_ENVIRONMENT_VARIABLE = "PICOPG_OPERATOR_TOKEN"
MIN_OPERATOR_TOKEN_BYTES = 16


def _markdown_request(assignment_id, markdown, base_version_id):
    """The canonical identity of one Assignment upload.

    Same idea as a bundle request: the content hash (not the raw bytes) plus the
    base decides whether two requests are the same logical publish.
    """

    return {
        "assignmentId": assignment_id,
        "baseVersionId": base_version_id,
        "sha256": hashlib.sha256(markdown).hexdigest(),
    }


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
        elif routes.match_assignment_versions(path) is not None:
            self._post_assignment_versions(routes.match_assignment_versions(path))
        elif path == routes.SUBMISSIONS_PATH:
            self._post_submission()
        elif routes.allowed_methods(path) is not None:
            self._method_not_allowed()
        else:
            self._not_found()

    def _post_submission(self):
        """Accept one hand-in: ``CreateSubmission``.

        The client names the delivery, so a lost response is retried with the
        same ``submission_id`` and lands on the same row instead of a second
        one. The row is the record; the readable folder beside the database is a
        copy written after the transaction, so it can never make a stored
        hand-in fail.

        Refusals are stable codes rather than sentences: the caller turns them
        into words for whoever handed the work in.
        """

        payload = self._read_json(max_bytes=MAX_JSON_BODY_BYTES)
        if payload is None:
            return

        try:
            delivery = decode_submission_delivery(payload)
        except InvalidSubmissionRequestError:
            self._api_error(400, "invalid_submission")
            return

        repository = self.server.repository
        try:
            with self._database() as connection:
                refusal = self._submission_refusal(connection, repository, delivery)
                if refusal is not None:
                    status, code = refusal
                    self._api_error(status, code)
                    return

                with immediate_transaction(connection):
                    write = create_submission(connection, delivery)
        except RepositoryError:
            self._api_error(500, "internal_error")
            return

        if write.status == "conflict":
            self._api_error(409, "submission_conflict")
            return

        self._write_submitted_work(delivery, write.created_at)
        self._respond_json(
            200 if write.status == "existing_same" else 201,
            {
                "status": write.status,
                "submission": {
                    "id": delivery.submission_id,
                    "source_kind": delivery.source_kind,
                    "source_id": delivery.source_id,
                    "version_id": delivery.version_id,
                    "student": delivery.student,
                    "created_at": write.created_at,
                },
            },
        )

    def _submission_refusal(self, connection, repository, delivery):
        """``None`` when this hand-in may be stored, or ``(status, code)``.

        Which object the Version has to belong to comes from `source_kind`: a
        Playground and an Assignment each keep their own history, and a Version
        that belongs to neither is not something a hand-in can be based on.
        """

        if delivery.source_kind == "playground":
            source = get_playground(connection, delivery.source_id)
            if source is None:
                return 404, "invalid_version"
            if source.state == "stopped":
                return 409, "playground_stopped"
            known = commit_in_playground_history(
                repository, delivery.source_id, delivery.version_id
            )
        else:
            source = assignments_module.get_assignment(connection, delivery.source_id)
            if source is None:
                return 404, "invalid_version"
            if source.state == "stopped":
                return 409, "assignment_stopped"
            known = commit_in_assignment_history(
                repository, delivery.source_id, delivery.version_id
            )
        return None if known else (404, "invalid_version")

    def _write_submitted_work(self, delivery, created_at):
        """Write a folder per hand-in, for a human to open.

        The row is the record and the folder is a copy of it, so this never runs
        inside the transaction: an aborted delivery must not leave a folder
        behind, and a folder must not be able to fail a stored hand-in. It runs
        on a retry too, which is what makes a lost folder self-healing.
        """

        with self._database() as connection:
            ordinal = submission_ordinal(
                connection,
                delivery.student,
                delivery.submission_id,
                created_at,
            )
            write_submission_files(
                self.server.submissions_path,
                delivery.submission_id,
                delivery.student,
                delivery.source_id,
                delivery.version_id,
                created_at,
                dict(delivery.files),
                ordinal,
            )

    def _route_delete(self):
        path = urlsplit(self.path).path
        playground_id = routes.match_playground(path)
        assignment_id = routes.match_assignment(path)

        if assignment_id is not None:
            self._delete_assignment(assignment_id)
        elif playground_id is not None:
            self._delete_playground(playground_id)
        elif routes.allowed_methods(path) is not None:
            self._method_not_allowed()
        else:
            self._not_found()

    def _post_restore(self, playground_id):
        """Put an archived Playground back into the catalog.

        The counterpart of the `DELETE` on the same path. The Version it comes
        back on is the one it already had; a client finds it again on its next
        catalog read.
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
        restores it. The playground is marked ``state = stopped``, and the Git
        ref is left intact so history remains reachable — a client pinned to a
        Version can still read it.

        Re-archiving a stopped playground is a no-op: the row keeps the same
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
                    with immediate_transaction(connection):
                        # Drop the recorded publishes so an exact replay of an
                        # old idempotency key cannot report a 201 for a course
                        # nobody can read.
                        connection.execute(
                            "DELETE FROM idempotency_results "
                            "WHERE scope = ? AND endpoint = ?",
                            (
                                OPERATOR_SCOPE,
                                routes.playground_versions_endpoint(playground_id),
                            ),
                        )
                        connection.execute(
                            "UPDATE playgrounds SET state = 'stopped' WHERE id = ?",
                            (playground_id,),
                        )
        except InvalidRequestError:
            self._api_error(400, "invalid_request")
            return

        # the archive result: the status alone says whether this call
        # changed anything.
        self._respond_json(
            200, {"status": "no_op" if already_stopped else "accepted"}
        )

    def _get_catalog(self, head_only=False):
        """Return the current snapshot: every active Playground and its Version.

        A whole snapshot, not a paged list and not a delta: a client compares it
        with what it has installed, downloads what is missing or different, and
        treats a Playground the catalog no longer names as one it may not use.
        The Playgrounds are ordered by when they were first published, so a
        client that re-reads the catalog sees a stable list.
        """
        try:
            with self._database() as connection:
                playgrounds = self._active_playgrounds(connection)
                assignments = assignments_module.active_assignments(connection)
        except RepositoryError:
            self._api_error(500, "internal_error", head_only=head_only)
            return

        self._respond_json(
            200,
            {
                "protocol_version": PROTOCOL_VERSION,
                "minimum_client_protocol_version": MINIMUM_CLIENT_PROTOCOL_VERSION,
                "playgrounds": playgrounds,
                "assignments": assignments,
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
        # means "the first one", and the database decides whether that is true.
        # A header that is present but blank is the same as absent.
        base_version_id = self.headers.get(BASE_VERSION_HEADER, "").strip() or None
        # The publish id names the pending ref that protects the prepared
        # Version, so it has to exist before the Git work does. It is an
        # identifier for one attempt, not a domain event: recovery finds the
        # Version's Playground by asking the database.
        publish_id = uuid.uuid4().hex

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

        # A replay answers from storage before anything else happens. It has to
        # come first: the stored request may no longer be valid against the
        # world as it is now — a retry of a first publish carries no base, and
        # the Playground has one by then — and re-judging it would refuse a
        # request the server already accepted.
        try:
            with self._database() as connection:
                stored = idempotency_lookup(
                    connection,
                    OPERATOR_SCOPE,
                    routes.playground_versions_endpoint(playground_id),
                    request_key,
                    _bundle_request(playground_id, bundle, base_version_id),
                )
        except IdempotencyConflict:
            self._api_error(409, "idempotency_conflict")
            return
        except IdempotencyRequestError:
            self._api_error(400, "invalid_request")
            return
        if stored is not None:
            self._respond_json(*stored)
            return

        # Three phases (implementation.md §3), and the order is the point: the Git
        # work must not sit inside the database transaction, and the database
        # must decide before the history ref moves.
        #
        #   1. refuse anything the database alone can refuse, without building
        #      a commit at all (an unknown Playground is not one of them: the
        #      first publish of an id creates it, implementation.md §3（不存在的 Playground 由第一次发布带出来）)
        #   2. prepare the Version in Git, protected by a pending ref
        #   3. one short transaction: the compare-and-swap on the current Version
        #   4. converge the history ref onto what the database now says
        handle = self.server.repository
        prepared = None
        try:
            with self._database() as connection:
                row = require_publishable(connection, playground_id, base_version_id)
                observed_version_id = observed_version(row)
            prepared = publish_playground_prepare(
                handle,
                playground_id,
                bundle,
                _commit_metadata(),
                publish_id,
                observed_version_id,
            )
        except InvalidRequestError:
            self._api_error(400, "invalid_request")
            return
        except PlaygroundStoppedError:
            self._respond_json(409, {"status": "playground_stopped"})
            return
        except PlaygroundBaseChangedError:
            self._respond_json(409, {"status": "conflict"})
            return
        except RepositoryError:
            self._api_error(500, "internal_error")
            return

        # The database half. Everything that can fail between Prepare and
        # Finalize is channelled through one `finally`: a Version the database
        # did not accept must leave no trace, whatever stopped it — a mapped
        # refusal, a lost base, or a database error nobody anticipated.
        settled = False
        try:
            try:
                with self._database() as connection:
                    status, body = execute_idempotent(
                        connection,
                        OPERATOR_SCOPE,
                        routes.playground_versions_endpoint(playground_id),
                        request_key,
                        _bundle_request(playground_id, bundle, base_version_id),
                        lambda connection: self._accept_prepared(
                            connection, playground_id, prepared, base_version_id
                        ),
                    )
            except IdempotencyConflict:
                self._api_error(409, "idempotency_conflict")
                return
            except IdempotencyRequestError:
                self._api_error(400, "invalid_request")
                return
            except InvalidRequestError:
                self._api_error(400, "invalid_request")
                return
            except PlaygroundStoppedError:
                # the publish result: the Playground is not writable,
                # and retrying the same request will not change that.
                self._respond_json(409, {"status": "playground_stopped"})
                return
            except PlaygroundBaseChangedError:
                # `conflict`: the base is stale. Nothing was
                # written, and the publisher resolves it by reading the current
                # Version.
                self._respond_json(409, {"status": "conflict"})
                return
            except RepositoryError:
                self._api_error(500, "internal_error")
                return

            # Finalize. The database has committed, so the Version is accepted;
            # the history ref and the pending ref now follow it. A failure here
            # does not fail the request — the accept has happened and the next
            # start-up recovery converges the ref — but it is logged.
            if prepared is not None and not prepared.no_op:
                self._finish_publish(playground_id, prepared.publish_id)
            settled = True
            self._respond_json(status, body)
        finally:
            if not settled and prepared is not None and not prepared.no_op:
                self._abandon_prepared(prepared.publish_id)

    def _abandon_prepared(self, publish_id):
        """Drop the pending ref of a Version the database did not accept.

        The commit itself is left to the Git GC: nothing references it once this
        returns, which is exactly the point of the pending ref existing.
        """
        try:
            delete_pending_ref(self.server.repository, publish_id)
        except RepositoryError as error:
            # Start-up recovery sweeps anything left behind, so this is loud but
            # not fatal.
            print(f"[picopg] pending ref not removed: {error}", flush=True)

    def _finish_publish(self, playground_id, publish_id):
        """Converge the history ref and drop the pending ref, best effort."""

        handle = self.server.repository
        try:
            with self._database() as connection:
                row = get_playground(connection, playground_id)
            if row is not None and row.current_version_id:
                converge_history_ref(handle, playground_id, row.current_version_id)
        except (RepositoryError, InvalidRequestError) as error:
            print(f"[picopg] history ref not converged: {error}", flush=True)
        try:
            delete_pending_ref(handle, publish_id)
        except RepositoryError as error:
            print(f"[picopg] pending ref not removed: {error}", flush=True)

    def _post_assignment_versions(self, assignment_id):
        """Publish one Assignment: a Markdown body becomes an immutable Version.

        The same three phases as a Playground — Git prepare, one short
        transaction, Git finalize — over the assignment ref namespace. There is
        no bundle to validate: the body *is* the content, and the only shape it
        has to satisfy is a title on the first line.
        """

        if not self._require_operator():
            return
        request_key = self._idempotency_key()
        if request_key is None:
            return

        base_version_id = self.headers.get(BASE_VERSION_HEADER, "").strip() or None
        publish_id = uuid.uuid4().hex

        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._api_error(400, "invalid_request")
            return
        if content_length < 0:
            self._api_error(400, "invalid_request")
            return
        if content_length > assignments_module.MAX_MARKDOWN_BYTES:
            self._api_error(413, "request_too_large")
            return

        markdown = self.rfile.read(content_length)
        try:
            title = assignments_module.decode_markdown(markdown)
            with self._database() as connection:
                stored = idempotency_lookup(
                    connection,
                    OPERATOR_SCOPE,
                    routes.assignment_versions_endpoint(assignment_id),
                    request_key,
                    _markdown_request(assignment_id, markdown, base_version_id),
                )
        except assignments_module.InvalidAssignmentError:
            self._api_error(400, "invalid_assignment")
            return
        except IdempotencyConflict:
            self._api_error(409, "idempotency_conflict")
            return
        except IdempotencyRequestError:
            self._api_error(400, "invalid_request")
            return
        if stored is not None:
            self._respond_json(*stored)
            return

        handle = self.server.repository
        prepared = None
        try:
            with self._database() as connection:
                row = assignments_module.require_publishable(
                    connection, assignment_id, base_version_id
                )
                observed_version_id = assignments_module.observed_version(row)
            prepared = prepare_assignment_version(
                handle,
                assignment_id,
                markdown,
                _commit_metadata(),
                publish_id,
                observed_version_id,
            )
        except assignments_module.InvalidAssignmentError:
            self._api_error(400, "invalid_assignment")
            return
        except assignments_module.AssignmentBaseChangedError:
            self._respond_json(409, {"status": "conflict"})
            return
        except RepositoryError:
            self._api_error(500, "internal_error")
            return

        settled = False
        try:
            try:
                with self._database() as connection:
                    status, body = execute_idempotent(
                        connection,
                        OPERATOR_SCOPE,
                        routes.assignment_versions_endpoint(assignment_id),
                        request_key,
                        _markdown_request(assignment_id, markdown, base_version_id),
                        lambda connection: self._accept_prepared_assignment(
                            connection, assignment_id, prepared, base_version_id, title
                        ),
                    )
            except IdempotencyConflict:
                self._api_error(409, "idempotency_conflict")
                return
            except IdempotencyRequestError:
                self._api_error(400, "invalid_request")
                return
            except assignments_module.InvalidAssignmentError:
                self._api_error(400, "invalid_assignment")
                return
            except assignments_module.AssignmentBaseChangedError:
                self._respond_json(409, {"status": "conflict"})
                return
            except RepositoryError:
                self._api_error(500, "internal_error")
                return

            if prepared is not None and not prepared.no_op:
                self._finish_assignment_publish(assignment_id, prepared.publish_id)
            settled = True
            self._respond_json(status, body)
        finally:
            if not settled and prepared is not None and not prepared.no_op:
                self._abandon_prepared(prepared.publish_id)

    def _accept_prepared_assignment(self, connection, assignment_id, prepared, base_version_id, title):
        """The database half of an Assignment publish, inside its transaction.

        A stopped Assignment is accepted here too: this write is also what puts
        it back on the students' list.
        """

        result = assignments_module.commit_prepared_assignment(
            connection, assignment_id, prepared, base_version_id, title
        )
        return 201, {
            "status": "no_op" if prepared.no_op else "accepted",
            "version_id": result,
        }

    def _finish_assignment_publish(self, assignment_id, publish_id):
        """Converge the assignment ref and drop the pending ref, best effort."""

        handle = self.server.repository
        try:
            with self._database() as connection:
                row = assignments_module.get_assignment(connection, assignment_id)
            if row is not None and row.current_version_id:
                converge_assignment_ref(handle, assignment_id, row.current_version_id)
        except (RepositoryError, assignments_module.InvalidAssignmentError) as error:
            print(f"[picopg] assignment ref not converged: {error}", flush=True)
        try:
            delete_pending_ref(handle, publish_id)
        except RepositoryError as error:
            print(f"[picopg] pending ref not removed: {error}", flush=True)

    def _delete_assignment(self, assignment_id):
        """取消发布：把它从学生的目录里拿掉，历史一个字节都不动。"""

        if not self._require_operator():
            return
        try:
            with self._database() as connection:
                changed = assignments_module.unpublish(connection, assignment_id)
        except assignments_module.InvalidAssignmentError:
            self._api_error(400, "invalid_request")
            return
        except assignments_module.MissingAssignmentError:
            self._not_found()
            return

        self._respond_json(200, {"status": "accepted" if changed else "no_op"})

    def _accept_prepared(self, connection, playground_id, prepared, base_version_id):
        """The database half of a publish, inside the idempotency transaction.

        The result is the publish result: the protocol's own field
        names carry the outcome, so the publisher never has to read an HTTP
        status to know what happened.
        """
        result = commit_prepared_version(
            connection, playground_id, prepared, base_version_id
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
        # A publish that was interrupted leaves a pending ref behind. Settle
        # every one of them before the first request: a pending ref that turned
        # out to be unaccepted must not stay reachable, and one that turned out
        # to be accepted must leave the history ref converged.
        recover_pending_refs(connection, repository)

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
