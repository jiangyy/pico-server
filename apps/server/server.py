"""PicoPG HTTP service using only the Python standard library.

Two things live here.  Reads hand a client the published course content it
asks for: the catalog, one commit's manifest, one file's exact bytes, and the
shared runtime files.  Writes accept a bundle from the operator and a bounded
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

import artifacts
import generated
import manifests
import pagination
import permissions
from bundles import (
    MAX_ARCHIVE_BYTES,
    MAX_TOTAL_BYTES,
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
    create_playground,
    get_playground,
    publish_playground,
)
from repository import (
    DEFAULT_REPOSITORY_PATH,
    CommitMetadata,
    PlaygroundHeadChangedError,
    RepositoryError,
    open_repository,
    retire_playground,
    valid_playground_id,
)
from submissions import (
    InvalidSubmissionRequestError,
    MissingSubmissionError,
    StoredSubmissionError,
    UnknownPlaygroundError,
    create_submission,
    decode_submission_request,
    get_submission,
    list_submissions,
    submission_ordinal,
    submission_to_mapping,
    write_submission_files,
)


HOST = "127.0.0.1"
PORT = 8001
DATABASE_PATH = Path("data/app.db")
ARTIFACTS_DIRECTORY_NAME = "artifacts"
GENERATED_DIRECTORY_NAME = "generated"
RUNTIME_DIRECTORY_NAME = "runtime"
SUBMISSIONS_DIRECTORY_NAME = "submissions"
BOOTSTRAP_PATH = "/api/v1/bootstrap"
COURSES_PATH = "/courses"

ARTIFACTS_PATH = "/api/v1/artifacts"
MAX_JSON_BODY_BYTES = 16 * 1024

COURSE_FILE_CACHE_CONTROL = "public, max-age=31536000, immutable"


# The policy every ``/courses/**`` response carries.  A course page is one
# published HTML file that carries its **own inline ``<script>`` and
# ``<style>``** (there is no separate asset pipeline to move them into files),
# and Pyodide compiles its WASM under this same policy.  So ``script-src``
# needs ``'unsafe-inline'`` for the page's own script and ``'wasm-unsafe-eval'``
# for the WASM compile, and ``connect-src 'self'`` lets Pyodide fetch
# ``pyodide.asm.wasm`` and the wheel from this origin.  This is deliberately the
# same policy as the desktop host's ``COURSE_CSP``; a stricter one silently
# renders an empty page, which is invisible to a status-code assertion.
COURSE_CSP = (
    "default-src 'self'; img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
    "connect-src 'self'"
)

ALLOWED_METHODS = ("GET", "HEAD")
PLAYGROUNDS_PATH = "/api/v1/playgrounds"
PLAYGROUND_COMMITS_SUFFIX = "/commits"
COMMITS_SEGMENT = "commits"
FILES_SEGMENT = "files"
MANIFEST_SEGMENT = "manifest"
CATALOG_PATH = "/api/v1/catalog"
SUBMISSIONS_PATH = "/api/v1/submissions"

# The idempotency scope of the single operator that publishes bundles.  A
# submission uses the self-reported student name instead, so two students can
# use the same key without colliding.
OPERATOR_SCOPE = "operator"

# A submission body is bounded JSON: the request fields plus text files, and it
# must stay inside the file-set limits the submission module enforces.  Twice
# the decoded total leaves room for JSON escaping.
MAX_SUBMISSION_JSON_BODY_BYTES = 2 * MAX_TOTAL_BYTES

# The operator token is the whole write authorization: publishing a bundle and
# reading what was handed in.  It is supplied at start-up, compared in constant
# time, and never stored in the database, so this service holds no account,
# no session, and no password.
OPERATOR_ENVIRONMENT_VARIABLE = "PICOPG_OPERATOR_TOKEN"
MIN_OPERATOR_TOKEN_BYTES = 16


def _match_playground_commit(path):
    """Return the playground ID in a commit upload path, else None."""
    prefix = PLAYGROUNDS_PATH + "/"
    if not path.startswith(prefix) or not path.endswith(
        PLAYGROUND_COMMITS_SUFFIX
    ):
        return None

    playground_id = path[len(prefix): -len(PLAYGROUND_COMMITS_SUFFIX)]
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


def _playground_commits_endpoint(playground_id):
    """The idempotency endpoint of a Playground's bundle publish.

    One spelling, used by both the publish that records a result and the retire
    that has to forget it, so the two can never drift apart.
    """

    return f"POST {PLAYGROUNDS_PATH}/{playground_id}{PLAYGROUND_COMMITS_SUFFIX}"


def _commit_route_parts(path):
    """Split ``.../<playground>/commits/<commit>/<tail>`` for read routes.

    Returns ``(playground_id, commit_id, tail)`` where ``tail`` is
    everything after ``<commit>/`` and may still contain literal slashes.
    Returns ``None`` for any other shape, so a missing or extra segment never
    matches.
    """

    prefix = PLAYGROUNDS_PATH + "/"
    if not path.startswith(prefix):
        return None

    parts = path[len(prefix):].split("/", 3)
    if len(parts) != 4:
        return None
    playground_id, segment, commit_id, tail = parts
    if segment != COMMITS_SEGMENT or not playground_id or not commit_id:
        return None
    return playground_id, commit_id, tail


def _match_manifest(path):
    """Return ``(playground_id, commit_id)`` for a manifest read path."""

    parts = _commit_route_parts(path)
    if parts is None:
        return None
    playground_id, commit_id, tail = parts
    if tail != MANIFEST_SEGMENT:
        return None
    return playground_id, commit_id


def _match_commit_files(path):
    """Return ``(playground_id, commit_id, encoded_path)`` for a file path."""

    parts = _commit_route_parts(path)
    if parts is None:
        return None
    playground_id, commit_id, tail = parts
    prefix = FILES_SEGMENT + "/"
    if not tail.startswith(prefix):
        return None
    encoded_path = tail[len(prefix):]
    if not encoded_path:
        return None
    return playground_id, commit_id, encoded_path


def _match_submission(path):
    """Return the opaque submission ID in a submission path, else None."""
    prefix = SUBMISSIONS_PATH + "/"
    if not path.startswith(prefix):
        return None

    submission_id = path[len(prefix):]
    if not submission_id or "/" in submission_id:
        return None
    return submission_id


def _match_artifact_version(path):
    """Return the single decoded version segment for an artifact path.

    The version is taken from the raw path exactly once; an encoded slash or
    backslash stays literal text and can never become a path separator.
    """

    prefix = ARTIFACTS_PATH + "/"
    if not path.startswith(prefix):
        return None
    version = path[len(prefix):]
    if not version or "/" in version:
        return None
    return version


def _match_course_file(path):
    """Return ``(playground_id, commit_id, encoded_path)`` for course files."""

    prefix = COURSES_PATH + "/"
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix):].split("/", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return tuple(parts)


def _allowed_methods(path):
    """Return the methods a known path accepts, or ``None`` when unknown."""
    if path in ("/healthz", BOOTSTRAP_PATH, ARTIFACTS_PATH, CATALOG_PATH):
        return ALLOWED_METHODS
    if path == SUBMISSIONS_PATH:
        return ALLOWED_METHODS
    if path == PLAYGROUNDS_PATH:
        return ("POST",)
    if _match_playground(path) is not None:
        return ("DELETE",)
    if _match_playground_commit(path) is not None:
        return ("POST",)
    if _match_course_file(path) is not None:
        return ALLOWED_METHODS
    if _match_artifact_version(path) is not None:
        return ALLOWED_METHODS
    if _match_manifest(path) is not None:
        return ALLOWED_METHODS
    if _match_commit_files(path) is not None:
        return ALLOWED_METHODS
    if _match_submission(path) is not None:
        return ALLOWED_METHODS
    return None


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


def _content_disposition(filename):
    """Return a safe attachment disposition for an indexed artifact name."""

    safe = "".join(
        character
        for character in filename
        if character.isascii()
        and (character.isalnum() or character in "._-")
    )
    if not safe:
        safe = "artifact"
    return f'attachment; filename="{safe}"'


# Suffix -> media type for the files a course bundle and the runtime contain.
# The image types matter: a card cover is a plain ``<img src>``, and this Server
# sends ``X-Content-Type-Options: nosniff``, so an unlisted suffix would be
# handed to the browser as an opaque download.
COURSE_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/vnd.microsoft.icon",
    ".wasm": "application/wasm",
    ".zip": "application/zip",
    ".whl": "application/zip",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
}


def _course_content_type(path):
    suffix = path.rpartition(".")[2].lower()
    return COURSE_CONTENT_TYPES.get(f".{suffix}", "application/octet-stream")


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
    """Route the public reads and the two writes."""

    def do_GET(self):
        self._dispatch(self._route_get)

    def do_HEAD(self):
        self._dispatch(self._route_get, head_only=True)

    def do_POST(self):
        self._dispatch(self._route_post)

    # The service has no PUT, PATCH, or DELETE.  Implementing them as the
    # shared method-not-allowed answer keeps one 405 vocabulary instead of the
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

        if _match_course_file(path) is not None:
            self._get_course_file(*_match_course_file(path), head_only=head_only)
        elif path == "/healthz":
            self._respond(200, "ok\n", head_only=head_only)
        elif path == BOOTSTRAP_PATH:
            self._get_bootstrap(head_only=head_only)
        elif path == ARTIFACTS_PATH:
            self._get_artifact_index(head_only=head_only)
        elif _match_artifact_version(path) is not None:
            self._get_artifact(
                _match_artifact_version(path), head_only=head_only
            )
        elif path == CATALOG_PATH:
            self._get_catalog(head_only=head_only)
        elif _match_manifest(path) is not None:
            self._get_manifest(*_match_manifest(path), head_only=head_only)
        elif _match_commit_files(path) is not None:
            self._get_commit_file(
                *_match_commit_files(path), head_only=head_only
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
        elif _match_playground_commit(path) is not None:
            self._post_playground_commit(_match_playground_commit(path))
        elif path == SUBMISSIONS_PATH:
            self._post_submission()
        elif _allowed_methods(path) is not None:
            self._method_not_allowed()
        else:
            self._not_found()

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

        Students stop seeing it on their next refresh; nothing else changes.  The
        database row stays (the submissions that reference it keep a valid
        parent) and the commits stay in the repository, so publishing the same id
        again restores it.  Reading a retired commit is already refused, because
        every read path resolves the ref first.

        The recorded publishes of this Playground are dropped, because a recorded
        result is only true while the ref it describes is still there: an exact
        replay answers with the stored response without touching Git, so an old
        idempotency key would report a 201 for a course nobody can read.  The rows
        go before the ref does, so a failure here leaves the Playground published
        rather than silently unrepeatable.
        """

        if not self._require_operator():
            return

        try:
            with self._database() as connection:
                stored = get_playground(connection, playground_id)
                if stored is not None:
                    with connection:
                        connection.execute(
                            "DELETE FROM idempotency_results "
                            "WHERE scope = ? AND endpoint = ?",
                            (
                                OPERATOR_SCOPE,
                                _playground_commits_endpoint(playground_id),
                            ),
                        )
        except InvalidRequestError:
            self._api_error(400, "invalid_request")
            return

        if stored is None:
            self._not_found()
            return

        try:
            retired = retire_playground(self.server.repository, playground_id)
        except PlaygroundHeadChangedError:
            self._api_error(409, "playground_head_changed")
            return
        except RepositoryError:
            self._api_error(500, "internal_error")
            return

        if not self._refresh_generated():
            # Authority is the ref, which is already gone; the next read rebuilds.
            pass
        self._respond_json(
            200, {"playgroundId": playground_id, "retired": retired}
        )

    def _current_generation(self):
        """Resolve the current generation, rebuilding at most once."""

        try:
            return generated.resolve_current_generation(
                self.server.generated_path
            )
        except generated.GeneratedCacheError:
            pass
        try:
            with self._database() as connection:
                generated.rebuild(
                    self.server.generated_path,
                    connection=connection,
                    repository=self.server.repository,
                    artifacts_path=self.server.artifacts_path,
                )
            return generated.resolve_current_generation(
                self.server.generated_path
            )
        except Exception:
            return None

    def _get_bootstrap(self, head_only=False):
        self._respond_generation_file(
            generated.BOOTSTRAP_FILENAME, head_only=head_only
        )

    def _get_artifact_index(self, head_only=False):
        self._respond_generation_file(
            generated.ARTIFACT_INDEX_FILENAME, head_only=head_only
        )

    def _respond_generation_file(self, name, head_only=False):
        generation = self._generation(head_only)
        if generation is None:
            return
        data = (
            generation.bootstrap
            if name == generated.BOOTSTRAP_FILENAME
            else generation.artifact_index
        )
        self._respond_bytes(
            200,
            data,
            content_type="application/json; charset=utf-8",
            etag=hashlib.sha256(data).hexdigest(),
            head_only=head_only,
        )

    def _generation(self, head_only=False):
        """Return the current generation, or answer 503 and return ``None``.

        Both generated-cache reads start here: the cache is a derived copy, so
        an unavailable one is a service problem (503), never a 404.
        """

        if self._reject_query_or_body(head_only=head_only):
            return None
        generation = self._current_generation()
        if generation is None:
            self._api_error(
                503, "generated_unavailable", head_only=head_only
            )
            return None
        return generation

    def _get_artifact(self, version, head_only=False):
        generation = self._generation(head_only)
        if generation is None:
            return
        try:
            entries = artifacts.mapping_to_index(
                json.loads(generation.artifact_index)
            )
        except (ValueError, artifacts.ArtifactError):
            self._api_error(
                503, "generated_unavailable", head_only=head_only
            )
            return
        entry = artifacts.entries_by_version(entries).get(version)
        if entry is None:
            self._api_error(404, "not_found", head_only=head_only)
            return
        try:
            target = artifacts.verify_artifact(
                self.server.artifacts_path, entry
            )
        except artifacts.ArtifactError:
            self._api_error(
                503, "artifact_unavailable", head_only=head_only
            )
            return
        self._respond_bytes(
            200,
            None,
            content_type="application/octet-stream",
            etag=entry.sha256,
            extra_headers=(
                (
                    "Content-Disposition",
                    _content_disposition(entry.filename),
                ),
            ),
            stream_path=target,
            stream_size=entry.size,
            head_only=head_only,
        )

    def _respond_bytes(
        self,
        status,
        data,
        *,
        content_type,
        etag=None,
        extra_headers=(),
        stream_path=None,
        stream_size=None,
        cache_control="no-store",
        head_only=False,
    ):
        if etag is not None and self._if_none_match(etag):
            self._respond_not_modified(head_only=head_only)
            return
        length = stream_size if stream_path is not None else len(data)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        if etag is not None:
            self.send_header("ETag", f'"{etag}"')
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self._headers_sent = True
        if not head_only:
            if stream_path is not None:
                with open(stream_path, "rb") as handle:
                    for chunk in iter(
                        lambda: handle.read(1024 * 1024), b""
                    ):
                        self.wfile.write(chunk)
            else:
                self.wfile.write(data)

    def _if_none_match(self, etag):
        header = self.headers.get("If-None-Match")
        return header is not None and header.strip() == f'"{etag}"'

    def _respond_not_modified(self, head_only=False):
        self.send_response(304)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._headers_sent = True

    def _get_course_file(
        self, playground_id, commit_id, encoded_path, head_only=False
    ):
        path = _decode_content_path(encoded_path)
        if path is None:
            self._not_found(head_only=head_only)
            return

        runtime = (
            path in ("pico.css", "pico.js", "world.js", "pico.svg")
            or path.startswith("libs/")
            or path.startswith("pyodide/")
        )
        if runtime:
            # The shared runtime is not part of a bundle, so it is served from
            # the runtime directory instead of a commit tree.  The path is
            # resolved inside that directory, so no request can escape it.
            segments = path.split("/")
            if any(not segment or segment in (".", "..") for segment in segments):
                self._not_found(head_only=head_only)
                return
            target = (self.server.runtime_path / path).resolve()
            root = self.server.runtime_path.resolve()
            if root not in target.parents or not target.is_file():
                self._not_found(head_only=head_only)
                return
            data = target.read_bytes()
            self._respond_bytes(
                200,
                data,
                content_type=_course_content_type(path),
                etag=hashlib.sha256(data).hexdigest(),
                cache_control=COURSE_FILE_CACHE_CONTROL,
                extra_headers=(("Content-Security-Policy", COURSE_CSP),),
                head_only=head_only,
            )
            return

        try:
            content = manifests.read_commit_file(
                self.server.repository, playground_id, commit_id, path
            )
        except manifests.ManifestError:
            self._not_found(head_only=head_only)
            return
        except RepositoryError:
            self._api_error(500, "internal_error", head_only=head_only)
            return
        self._respond_bytes(
            200,
            content.data,
            content_type=_course_content_type(path),
            etag=content.sha256,
            cache_control=COURSE_FILE_CACHE_CONTROL,
            extra_headers=(("Content-Security-Policy", COURSE_CSP),),
            head_only=head_only,
        )

    def _get_catalog(self, head_only=False):
        parsed = self._list_query(pagination.CATALOG_ROUTE, head_only)
        if parsed is None:
            return
        limit, after = parsed

        try:
            with self._database() as connection:
                page = self._build_catalog_page(connection, limit, after)
        except RepositoryError:
            self._api_error(500, "internal_error", head_only=head_only)
            return

        self._respond_json(
            200,
            {
                "catalog": list(page.items),
                "nextCursor": self._next_cursor(
                    pagination.CATALOG_ROUTE, page.next_key
                ),
            },
            head_only=head_only,
        )

    def _build_catalog_page(self, connection, limit, after):
        """Return one bounded page of published Playground commits.

        The order is the order a teacher published the courses in
        (``created_at``, then ``id`` as the tie-break), which is also the cursor's
        key.  The page is read as an ordered batch of ``limit + 1`` rows and
        trimmed to ``limit``, so the cursor is stable and a page never grows past
        the caller's limit.  A playground whose id violates the identifier rules
        fails closed instead of being skipped silently.
        """

        after_key = tuple(after) if after is not None else None

        items = []
        next_key = None
        while len(items) < limit:
            where = ""
            parameters = []
            if after_key is not None:
                # Row-value comparison: the ordering key and the cursor key are the
                # same pair, so a page can neither skip nor repeat a playground.
                where = " WHERE (created_at, id) > (?, ?)"
                parameters.extend(after_key)
            parameters.append(limit + 1 - len(items))
            rows = connection.execute(
                f"SELECT id, created_at FROM playgrounds{where} "
                "ORDER BY created_at, id LIMIT ?",
                parameters,
            ).fetchall()
            if not rows:
                break
            for row in rows:
                # The cursor follows the last row *read*, not the last one returned:
                # a row skipped for having no commit must not be offered again.
                after_key = (row["created_at"], row["id"])
                if not valid_playground_id(row["id"]):
                    raise RepositoryError(
                        "stored playground identifier is invalid"
                    )
                commit_id = manifests.current_playground_commit(
                    self.server.repository, row["id"]
                )
                if commit_id is None:
                    continue
                items.append({"playgroundId": row["id"], "commitId": commit_id})
                if len(items) >= limit:
                    break

        if len(items) >= limit and next_key is None and after_key is not None:
            next_key = list(after_key)
        return pagination.Page(items=tuple(items), next_key=next_key)

    def _get_manifest(self, playground_id, commit_id, head_only=False):
        try:
            manifest = manifests.generate_manifest(
                self.server.repository, playground_id, commit_id
            )
        except manifests.ManifestError:
            self._not_found(head_only=head_only)
            return
        except RepositoryError:
            self._api_error(500, "internal_error", head_only=head_only)
            return

        self._respond_json(
            200,
            {"manifest": manifests.manifest_to_mapping(manifest)},
            head_only=head_only,
        )

    def _get_commit_file(
        self, playground_id, commit_id, encoded_path, head_only=False
    ):
        path = _decode_content_path(encoded_path)
        if path is None:
            self._not_found(head_only=head_only)
            return

        try:
            content = manifests.read_commit_file(
                self.server.repository, playground_id, commit_id, path
            )
        except manifests.ManifestError:
            self._not_found(head_only=head_only)
            return
        except RepositoryError:
            self._api_error(500, "internal_error", head_only=head_only)
            return

        self._respond_bytes(
            200,
            content.data,
            content_type="application/octet-stream",
            etag=content.sha256,
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

    def _post_playground_commit(self, playground_id):
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
                    _playground_commits_endpoint(playground_id),
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
        except PlaygroundHeadChangedError:
            self._api_error(409, "playground_head_changed")
            return
        except RepositoryError:
            self._api_error(500, "internal_error")
            return

        if not self._refresh_generated():
            # Authority is already committed, so a cache failure must not change
            # the publish result.  The next read rebuilds from authority.
            pass
        self._respond_json(status, body)

    def _publish_bundle(self, connection, playground_id, bundle):
        result = publish_playground(
            connection,
            playground_id,
            self.server.repository,
            bundle,
            _commit_metadata(),
        )
        return 201, {
            "playgroundId": result.playground_id,
            "commitId": result.commit_id,
            "noOp": result.no_op,
        }

    def _refresh_generated(self):
        """Rebuild disposable caches after an authoritative mutation.

        Authority is already committed, so a cache write failure never rolls
        it back and never changes the mutation's response.  ``rebuild`` itself
        preserves the previous complete generation on failure.
        """

        try:
            with self._database() as connection:
                generated.rebuild(
                    self.server.generated_path,
                    connection=connection,
                    repository=self.server.repository,
                    artifacts_path=self.server.artifacts_path,
                )
            return True
        except Exception:
            return False

    def _post_submission(self):
        """Accept one bounded submission from a client.

        This is the only anonymous write.  The body is validated field by field
        before any database or Git access, and the named Playground commit must
        be published, so a submission can never reference content this server
        does not serve.
        """

        request_key = self._idempotency_key()
        if request_key is None:
            return

        payload = self._read_json(max_bytes=MAX_SUBMISSION_JSON_BODY_BYTES)
        if payload is None:
            return

        try:
            request = decode_submission_request(payload)
        except InvalidSubmissionRequestError as error:
            self._api_error(
                400,
                "invalid_request",
                details={"reason": str(error)},
            )
            return

        ordinal = 1
        try:
            with self._database() as connection:
                status, body = create_submission(
                    connection,
                    self.server.repository,
                    request,
                    request_key,
                )
                # The folder number is the submission's position in the record, so
                # a replayed create names the same folder as the first attempt.
                submission = body.get("submission") if isinstance(body, dict) else None
                if status == 201 and submission is not None:
                    ordinal = submission_ordinal(
                        connection, request.student, submission["id"], submission["createdAt"]
                    )
        except IdempotencyConflict:
            self._api_error(409, "idempotency_conflict")
            return
        except IdempotencyRequestError:
            self._api_error(400, "invalid_request")
            return
        except UnknownPlaygroundError:
            self._api_error(404, "not_found")
            return
        except InvalidSubmissionRequestError:
            self._api_error(400, "invalid_request")
            return

        # The row is the record; the files are the copy a human opens. Writing
        # after the row exists means a storage failure is a failed request the
        # client can retry, and a replay writes the same bytes to the same
        # path, so the retry is harmless.
        if status == 201:
            submission = body.get("submission") if isinstance(body, dict) else None
            try:
                write_submission_files(
                    self.server.submissions_path,
                    submission["id"],
                    request.student,
                    request.playground_id,
                    request.playground_commit_id,
                    submission["createdAt"],
                    dict(request.files),
                    ordinal=ordinal,
                )
            except (KeyError, TypeError, OSError, permissions.PermissionPolicyError):
                self._api_error(500, "submission_storage_failed")
                return

        self._respond_json(status, body)

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

    def _reject_query_or_body(self, head_only=False):
        if urlsplit(self.path).query:
            self._api_error(400, "invalid_request", head_only=head_only)
            return True
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return False
        try:
            length = int(raw_length)
        except ValueError:
            self._api_error(400, "invalid_request", head_only=head_only)
            return True
        if length > 0:
            self._api_error(400, "invalid_request", head_only=head_only)
            return True
        return False

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


def _initialize_generated(server):
    """Build the first complete generation, or fail startup closed.

    Startup never runs with absent bootstrap data.  ``generated.rebuild``
    preserves a previously valid generation when a rebuild fails, but startup
    still fails closed so a broken authoritative source cannot be masked by a
    stale cache.
    """

    try:
        with closing(open_database(server.database_path)) as connection:
            generated.rebuild(
                server.generated_path,
                connection=connection,
                repository=server.repository,
                artifacts_path=server.artifacts_path,
            )
    except generated.GeneratedCacheError:
        raise
    except Exception as error:
        raise generated.GeneratedCacheError(
            "generated cache could not be initialized"
        ) from error


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
    artifacts_path=None,
    generated_path=None,
    runtime_path=None,
    submissions_path=None,
    operator_token=None,
):
    """Return an initialized HTTP server bound to ``host:port``.

    ``artifacts_path``, ``generated_path``, ``runtime_path`` and
    ``submissions_path`` are injected runtime directories.  When omitted they
    default to sibling directories of the database, which keeps every
    deployment path under one data directory.  The directories are created
    lazily by the artifact index, generated-cache code and submission writer
    instead of at startup, so a read-only or unused directory never becomes a
    start-up failure.  ``runtime_path`` contains the shared files loaded on
    demand by course pages; ``submissions_path`` holds one directory per
    handed-in piece of work.
    """
    operator_token = _validate_operator_token(operator_token)

    database_path = Path(database_path)
    database = open_database(database_path)
    database.close()

    if artifacts_path is None:
        artifacts_path = database_path.parent / ARTIFACTS_DIRECTORY_NAME
    else:
        artifacts_path = Path(artifacts_path)
    if generated_path is None:
        generated_path = database_path.parent / GENERATED_DIRECTORY_NAME
    else:
        generated_path = Path(generated_path)

    if runtime_path is None:
        runtime_path = database_path.parent / RUNTIME_DIRECTORY_NAME
    else:
        runtime_path = Path(runtime_path)

    if submissions_path is None:
        submissions_path = database_path.parent / SUBMISSIONS_DIRECTORY_NAME
    else:
        submissions_path = Path(submissions_path)

    repository_path = Path(repository_path)
    repository = open_repository(repository_path)

    if artifacts_path.exists():
        permissions.ensure_private_directory(artifacts_path)

    server = SlimHTTPServer((host, port), Handler)
    server.database_path = database_path
    server.repository_path = repository_path
    server.repository = repository
    server.artifacts_path = artifacts_path
    server.generated_path = generated_path
    server.runtime_path = runtime_path
    server.submissions_path = submissions_path
    server.operator_token = operator_token
    _initialize_generated(server)
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
