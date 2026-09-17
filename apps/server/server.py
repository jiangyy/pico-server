"""PicoPG HTTP service using only the Python standard library."""

import hashlib
import json
import threading
from contextlib import closing
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import audit
import artifacts
import authorization
import generated
import manifests
import pagination
import permissions
import submissions
import work_content
import works
from accounts import (
    ForbiddenActionError,
    InvalidRoleError,
    MissingUserError,
    RoleConflictError,
    StoredUserError,
    UsernameTakenError,
    change_user_role,
    create_user,
    list_users,
    register_student,
)
from bundles import (
    MAX_ARCHIVE_BYTES,
    BundleValidationError,
    decode_bundle_zip,
    validate_bundle,
)
from classes import (
    ArchivedClassError,
    InvalidClassNameError,
    MembershipNotFoundError,
    MissingClassError,
    RoleMismatchError,
    assign_student,
    change_class_owner,
    create_class,
    get_class,
    list_classes,
    list_memberships,
    remove_membership,
)
from comments import (
    MAX_COMMENT_BODY_BYTES,
    InvalidCommentRequestError,
    comment_to_mapping,
    create_comment,
    list_comments,
)
from database import open_database
from idempotency import (
    IdempotencyConflict,
    IdempotencyRequestError,
    execute as execute_idempotent,
)
from playgrounds import (
    PUBLIC_POLICY,
    InvalidPolicyError,
    InvalidRequestError,
    MissingPlaygroundError,
    PlaygroundConflictError,
    StoredPlaygroundError,
    create_playground,
    list_playgrounds,
    list_releasable_playground_ids,
    publish_playground,
    update_playground,
)
from releases import (
    InvalidReleaseRequestError,
    MissingReleaseError,
    UnknownPlaygroundCommitError,
    create_release,
    get_release,
    list_releases,
    release_to_mapping,
)
from repository import (
    DEFAULT_REPOSITORY_PATH,
    CommitMetadata,
    PlaygroundHeadChangedError,
    RepositoryError,
    WorkHeadChangedError,
    open_repository,
    valid_playground_id,
)
from sessions import (
    InvalidCredentialsError,
    InvalidSessionError,
    authenticate_session,
    login,
    revoke_session,
)
from submissions import (
    InvalidSubmissionRequestError,
    MissingSubmissionError,
    SubmissionIntegrityError,
    SubmissionNotAllowedError,
    UnknownReleaseError,
    UnknownWorkCommitError,
    create_submission,
    get_submission,
    list_submissions,
    review_submission,
    submission_to_mapping,
)


HOST = "127.0.0.1"
PORT = 8001
DATABASE_PATH = Path("data/app.db")
ARTIFACTS_DIRECTORY_NAME = "artifacts"
GENERATED_DIRECTORY_NAME = "generated"
WEB_DIRECTORY_NAME = "web"
WEB_RUNTIME_DIRECTORY_NAME = "runtime"
BOOTSTRAP_PATH = "/api/v1/bootstrap"
COURSES_PATH = "/courses"
SESSION_COOKIE_NAME = "picopg_session"
SESSION_COOKIE_MAX_AGE = 30 * 24 * 60 * 60

# The Web bundle is four files: one startup document plus the three assets it
# references.  Serving is a closed table rather than a directory walk, so no
# caller-supplied name is ever joined onto a path and no other file in the
# directory can become reachable by guessing a URL.  A missing bundle keeps
# the historical placeholder for ``/`` so an un-provisioned Server still
# answers its health probe, but every other bundle path is a plain 404.
WEB_BUNDLE = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/main.js": ("main.js", "text/javascript; charset=utf-8"),
    "/picopg.css": ("picopg.css", "text/css; charset=utf-8"),
    "/pico.svg": ("pico.svg", "image/svg+xml"),
}
WEB_BUNDLE_CACHE_CONTROL = "public, max-age=300"
ARTIFACTS_PATH = "/api/v1/artifacts"
MAX_JSON_BODY_BYTES = 16 * 1024
# A Comment body may use the full Comment-domain byte ceiling, and JSON string
# escaping can expand it several times over.  The comment route accepts a
# larger envelope and lets the Comment domain enforce the body's own limit.
MAX_COMMENT_JSON_BODY_BYTES = 6 * MAX_COMMENT_BODY_BYTES + 1024

ALLOWED_METHODS = ("GET", "HEAD")
ROUTE_METHODS = {
    "/": ALLOWED_METHODS,
    "/healthz": ALLOWED_METHODS,
    "/api/v1/me": ALLOWED_METHODS,
    "/api/v1/register": ("POST",),
    "/api/v1/login": ("POST",),
    "/api/v1/logout": ("POST",),
}
ADMIN_USERS_PATH = "/api/v1/admin/users"
ADMIN_USER_ROLE_SUFFIX = "/role"
ADMIN_CLASSES_PATH = "/api/v1/admin/classes"
ADMIN_PLAYGROUNDS_PATH = "/api/v1/admin/playgrounds"
CLASS_OWNER_SUFFIX = "/owner"
CLASS_MEMBERS_SEGMENT = "/members/"
CLASSES_PATH = "/api/v1/classes"
PLAYGROUNDS_PATH = "/api/v1/playgrounds"
PLAYGROUND_COMMITS_SUFFIX = "/commits"
WORKS_PATH = "/api/v1/works"
WORK_SYNC_SUFFIX = "/sync"
WORK_RESTORE_SUFFIX = "/restore"
WORK_HEAD_SUFFIX = "/head"
WORK_HISTORY_SUFFIX = "/history"
COMMITS_SEGMENT = "commits"
FILES_SEGMENT = "files"
MANIFEST_SEGMENT = "manifest"
CATALOG_PATH = "/api/v1/catalog"
RELEASES_PATH = "/api/v1/releases"
CLASS_RELEASES_SUFFIX = "/releases"
SUBMISSIONS_PATH = "/api/v1/submissions"
SUBMISSION_COMMENTS_SUFFIX = "/comments"
RELEASE_SUBMISSIONS_SUFFIX = "/submissions"
MAX_IDEMPOTENCY_KEY_LENGTH = 255

# Returned by optional authentication when an Authorization header was present
# but did not carry a valid session, so the caller can stop without confusing
# the failure with an anonymous request.
_AUTH_FAILED = object()


def _match_admin_user_role(path):
    """Return the opaque user ID in an admin role path, else None."""
    prefix = ADMIN_USERS_PATH + "/"
    if not path.startswith(prefix):
        return None

    remainder = path[len(prefix):]
    if not remainder.endswith(ADMIN_USER_ROLE_SUFFIX):
        return None

    user_id = remainder[: -len(ADMIN_USER_ROLE_SUFFIX)]
    if not user_id or "/" in user_id:
        return None
    return user_id


def _match_class_owner(path):
    """Return the opaque class ID in a class owner path, else None."""
    prefix = ADMIN_CLASSES_PATH + "/"
    if not path.startswith(prefix) or not path.endswith(CLASS_OWNER_SUFFIX):
        return None

    class_id = path[len(prefix): -len(CLASS_OWNER_SUFFIX)]
    if not class_id or "/" in class_id:
        return None
    return class_id


def _match_class_member(path):
    """Return the opaque (class ID, student ID) pair in a member path."""
    prefix = ADMIN_CLASSES_PATH + "/"
    if not path.startswith(prefix):
        return None

    remainder = path[len(prefix):]
    class_id, separator, student_id = remainder.partition(CLASS_MEMBERS_SEGMENT)
    if not separator or not class_id or not student_id:
        return None
    if "/" in class_id or "/" in student_id:
        return None
    return class_id, student_id


def _match_class(path):
    """Return the opaque class ID in a class read path, else None."""
    prefix = CLASSES_PATH + "/"
    if not path.startswith(prefix):
        return None

    class_id = path[len(prefix):]
    if not class_id or "/" in class_id:
        return None
    return class_id


def _match_class_members(path):
    """Return the opaque class ID in a members read path, else None."""
    prefix = CLASSES_PATH + "/"
    if not path.startswith(prefix):
        return None

    remainder = path[len(prefix):]
    class_id, separator, suffix = remainder.partition("/")
    if not class_id or separator != "/" or suffix != "members":
        return None
    return class_id


def _match_playground(path):
    """Return the opaque playground ID in a playground path, else None."""
    prefix = PLAYGROUNDS_PATH + "/"
    if not path.startswith(prefix):
        return None

    playground_id = path[len(prefix):]
    if not playground_id or "/" in playground_id:
        return None
    return playground_id


def _match_work_sync(path):
    """Return the opaque playground ID in a Work sync path, else None."""
    prefix = WORKS_PATH + "/"
    if not path.startswith(prefix) or not path.endswith(WORK_SYNC_SUFFIX):
        return None

    playground_id = path[len(prefix): -len(WORK_SYNC_SUFFIX)]
    if not playground_id or "/" in playground_id:
        return None
    return playground_id


def _match_work_restore(path):
    """Return the opaque playground ID in a Work restore path, else None."""
    prefix = WORKS_PATH + "/"
    if not path.startswith(prefix) or not path.endswith(WORK_RESTORE_SUFFIX):
        return None

    playground_id = path[len(prefix): -len(WORK_RESTORE_SUFFIX)]
    if not playground_id or "/" in playground_id:
        return None
    return playground_id


def _match_work_head(path):
    """Return the playground ID in a Work head path, else None."""
    prefix = WORKS_PATH + "/"
    if not path.startswith(prefix) or not path.endswith(WORK_HEAD_SUFFIX):
        return None

    playground_id = path[len(prefix): -len(WORK_HEAD_SUFFIX)]
    if not playground_id or "/" in playground_id:
        return None
    return playground_id


def _match_work_history(path):
    """Return the playground ID in a Work history path, else None."""
    prefix = WORKS_PATH + "/"
    if not path.startswith(prefix) or not path.endswith(
        WORK_HISTORY_SUFFIX
    ):
        return None

    playground_id = path[len(prefix): -len(WORK_HISTORY_SUFFIX)]
    if not playground_id or "/" in playground_id:
        return None
    return playground_id


def _work_commit_route_parts(path):
    """Split ``.../works/<playground>/commits/<commit>/<tail>``.

    Returns ``(playground_id, commit_id, tail)`` or ``None`` for any other
    shape, so a missing or extra segment never matches.
    """

    prefix = WORKS_PATH + "/"
    if not path.startswith(prefix):
        return None

    parts = path[len(prefix):].split("/", 3)
    if len(parts) != 4:
        return None
    playground_id, segment, commit_id, tail = parts
    if segment != COMMITS_SEGMENT or not playground_id or not commit_id:
        return None
    return playground_id, commit_id, tail


def _match_work_manifest(path):
    """Return ``(playground_id, commit_id)`` for a Work manifest path."""

    parts = _work_commit_route_parts(path)
    if parts is None:
        return None
    playground_id, commit_id, tail = parts
    if tail != MANIFEST_SEGMENT:
        return None
    return playground_id, commit_id


def _match_work_commit_files(path):
    """Return ``(playground_id, commit_id, encoded_path)`` for a Work file."""

    parts = _work_commit_route_parts(path)
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


def _match_class_releases(path):
    """Return the opaque class ID in a class release creation path."""
    prefix = CLASSES_PATH + "/"
    if not path.startswith(prefix) or not path.endswith(
        CLASS_RELEASES_SUFFIX
    ):
        return None

    class_id = path[len(prefix): -len(CLASS_RELEASES_SUFFIX)]
    if not class_id or "/" in class_id:
        return None
    return class_id


def _match_release(path):
    """Return the opaque release ID in a release read path, else None."""
    prefix = RELEASES_PATH + "/"
    if not path.startswith(prefix):
        return None

    release_id = path[len(prefix):]
    if not release_id or "/" in release_id:
        return None
    return release_id


def _match_release_submissions(path):
    """Return the opaque release ID in a release submission path."""
    prefix = RELEASES_PATH + "/"
    if not path.startswith(prefix) or not path.endswith(
        RELEASE_SUBMISSIONS_SUFFIX
    ):
        return None

    release_id = path[len(prefix): -len(RELEASE_SUBMISSIONS_SUFFIX)]
    if not release_id or "/" in release_id:
        return None
    return release_id


def _match_submission(path):
    """Return the opaque submission ID in a submission path, else None."""
    prefix = SUBMISSIONS_PATH + "/"
    if not path.startswith(prefix):
        return None

    submission_id = path[len(prefix):]
    if not submission_id or "/" in submission_id:
        return None
    return submission_id


def _match_submission_comments(path):
    """Return the opaque submission ID in a submission comment path."""
    prefix = SUBMISSIONS_PATH + "/"
    if not path.startswith(prefix) or not path.endswith(
        SUBMISSION_COMMENTS_SUFFIX
    ):
        return None

    submission_id = path[len(prefix): -len(SUBMISSION_COMMENTS_SUFFIX)]
    if not submission_id or "/" in submission_id:
        return None
    return submission_id


def _submission_work_route_parts(path):
    """Split ``.../submissions/<submission>/work/<tail>``.

    Returns ``(submission_id, tail)`` or ``None`` for any other shape, so a
    missing or extra segment never matches.
    """

    prefix = SUBMISSIONS_PATH + "/"
    if not path.startswith(prefix):
        return None

    parts = path[len(prefix):].split("/", 2)
    if len(parts) != 3:
        return None
    submission_id, segment, tail = parts
    if segment != "work" or not submission_id:
        return None
    return submission_id, tail


def _match_submission_work_manifest(path):
    """Return the opaque submission ID for a Work review manifest path."""

    parts = _submission_work_route_parts(path)
    if parts is None:
        return None
    submission_id, tail = parts
    if tail != MANIFEST_SEGMENT:
        return None
    return submission_id


def _match_submission_work_files(path):
    """Return ``(submission_id, encoded_path)`` for a Work review file."""

    parts = _submission_work_route_parts(path)
    if parts is None:
        return None
    submission_id, tail = parts
    prefix = FILES_SEGMENT + "/"
    if not tail.startswith(prefix):
        return None
    encoded_path = tail[len(prefix):]
    if not encoded_path:
        return None
    return submission_id, encoded_path


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
    methods = ROUTE_METHODS.get(path)
    if methods is not None:
        return methods
    if path == BOOTSTRAP_PATH:
        return ALLOWED_METHODS
    if path == ARTIFACTS_PATH:
        return ALLOWED_METHODS
    if _match_artifact_version(path) is not None:
        return ALLOWED_METHODS
    if path == ADMIN_USERS_PATH:
        return ("GET", "HEAD", "POST")
    if _match_admin_user_role(path) is not None:
        return ("PATCH",)
    if path == ADMIN_CLASSES_PATH:
        return ("POST",)
    if path == ADMIN_PLAYGROUNDS_PATH:
        return ALLOWED_METHODS
    if _match_class_owner(path) is not None:
        return ("PATCH",)
    if _match_class_member(path) is not None:
        return ("PUT", "DELETE")
    if path == CLASSES_PATH:
        return ALLOWED_METHODS
    if _match_class_members(path) is not None:
        return ALLOWED_METHODS
    if _match_class(path) is not None:
        return ALLOWED_METHODS
    if path == PLAYGROUNDS_PATH:
        return ("GET", "HEAD", "POST")
    if _match_course_file(path) is not None:
        return ALLOWED_METHODS
    if _match_playground_commit(path) is not None:
        return ("POST",)
    if _match_work_sync(path) is not None:
        return ("POST",)
    if _match_work_restore(path) is not None:
        return ("POST",)
    if _match_work_head(path) is not None:
        return ALLOWED_METHODS
    if _match_work_history(path) is not None:
        return ALLOWED_METHODS
    if _match_work_manifest(path) is not None:
        return ALLOWED_METHODS
    if _match_work_commit_files(path) is not None:
        return ALLOWED_METHODS
    if _match_manifest(path) is not None:
        return ALLOWED_METHODS
    if _match_commit_files(path) is not None:
        return ALLOWED_METHODS
    if _match_playground(path) is not None:
        return ("PATCH",)
    if _match_class_releases(path) is not None:
        return ("GET", "HEAD", "POST")
    if path == CATALOG_PATH:
        return ALLOWED_METHODS
    if path == SUBMISSIONS_PATH:
        return ALLOWED_METHODS
    if path == RELEASES_PATH:
        return ALLOWED_METHODS
    if _match_release_submissions(path) is not None:
        return ("GET", "HEAD", "POST")
    if _match_submission_comments(path) is not None:
        return ("GET", "HEAD", "POST")
    if _match_submission_work_manifest(path) is not None:
        return ALLOWED_METHODS
    if _match_submission_work_files(path) is not None:
        return ALLOWED_METHODS
    if _match_submission(path) is not None:
        return ALLOWED_METHODS
    if _match_release(path) is not None:
        return ALLOWED_METHODS
    return None


def _audit_action(method, path):
    """Return the ``(action, target)`` audited for a mutating request.

    Only authenticated mutations are named here; unauthenticated account
    creation and login return ``(None, None)``, as do all reads.  A read is
    still audited when it ends in an authorization failure.  The target is
    the opaque domain ID the mutation acts on, never a raw path or query.
    """

    if method == "POST":
        if path == ADMIN_USERS_PATH:
            return "admin.user.create", None
        if path == ADMIN_CLASSES_PATH:
            return "class.create", None
        if path == PLAYGROUNDS_PATH:
            return "playground.create", None
        if path == "/api/v1/logout":
            return "account.logout", None
        playground_id = _match_playground_commit(path)
        if playground_id is not None:
            return "playground.publish", playground_id
        playground_id = _match_work_sync(path)
        if playground_id is not None:
            return "work.sync", playground_id
        playground_id = _match_work_restore(path)
        if playground_id is not None:
            return "work.restore", playground_id
        class_id = _match_class_releases(path)
        if class_id is not None:
            return "release.create", class_id
        release_id = _match_release_submissions(path)
        if release_id is not None:
            return "submission.create", release_id
        submission_id = _match_submission_comments(path)
        if submission_id is not None:
            return "comment.create", submission_id
        return None, None

    if method == "PATCH":
        user_id = _match_admin_user_role(path)
        if user_id is not None:
            return "admin.user.role.update", user_id
        class_id = _match_class_owner(path)
        if class_id is not None:
            return "class.owner.update", class_id
        playground_id = _match_playground(path)
        if playground_id is not None:
            return "playground.update", playground_id
        return None, None

    if method in ("PUT", "DELETE"):
        member = _match_class_member(path)
        if member is not None:
            class_id, student_id = member
            action = (
                "class.member.add"
                if method == "PUT"
                else "class.member.remove"
            )
            return action, f"{class_id}/{student_id}"
    return None, None


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


def _course_content_type(path):
    if path.endswith(".html"):
        return "text/html; charset=utf-8"
    if path.endswith(".js") or path.endswith(".mjs"):
        return "text/javascript; charset=utf-8"
    if path.endswith(".css"):
        return "text/css; charset=utf-8"
    if path.endswith(".svg"):
        return "image/svg+xml"
    if path.endswith(".json"):
        return "application/json; charset=utf-8"
    if path.endswith(".wasm"):
        return "application/wasm"
    return "application/octet-stream"


def _commit_metadata(actor):
    """Build the trusted, server-owned commit identity for an actor."""

    return CommitMetadata(
        name=actor.username,
        email=f"{actor.id}@picopg.invalid",
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        message="Publish playground bundle",
    )


class Handler(BaseHTTPRequestHandler):
    """Route public pages and the first account API."""

    def do_GET(self):
        self._dispatch(self._route_get)

    def do_HEAD(self):
        self._dispatch(self._route_get, head_only=True)

    def do_POST(self):
        self._dispatch(self._route_post)

    def do_PATCH(self):
        self._dispatch(self._route_patch)

    def _dispatch(self, route, **kwargs):
        """Run one request and emit its audit event after the response."""

        log = getattr(self.server, "audit", None)
        self._headers_sent = False
        if log is None:
            try:
                route(**kwargs)
            except Exception:
                self._handle_unexpected_failure()
            return

        self._request_id = log.request_id(
            self.headers.get("X-Request-Id"), secret=self._bearer_token()
        )
        self._audit_actor_id = None
        self._audit_status = None
        self._audit_code = None
        self._audit_override = None
        self._audit_recorded = False
        started_at = log.now()
        context = getattr(self.server, "_audit_context", None)
        if context is not None:
            # ``handle_error`` runs on this same request thread, so it can
            # recover the in-flight request identity if the route aborts.
            context.handler = self
        try:
            route(**kwargs)
        except Exception:
            self._handle_unexpected_failure()
        finally:
            self._emit_audit(log, started_at)

    def _handle_unexpected_failure(self):
        """Turn an unexpected route exception into one bounded response.

        Once response headers are committed no second response is possible,
        so the connection is aborted instead.  The single audit event is left
        to the completed-response path or to :meth:`_emit_audit`.
        """

        if self._headers_sent:
            self.close_connection = True
            return
        self._audit_code = "internal_error"
        try:
            self._api_error(500, "internal_error")
        except Exception:
            self.close_connection = True

    def _emit_audit(self, log, started_at):
        status = self._audit_status
        if status is None:
            return

        if self._audit_override is not None:
            action, target = self._audit_override
        else:
            path = urlsplit(self.path).path
            action, target = _audit_action(self.command, path)
            if action is None:
                # Unauthenticated login failures carry no actor or target
                # worth auditing; every other authorization failure and every
                # server error is a bounded event.
                if status >= 500:
                    action = "request.error"
                elif status in (401, 403) and path != "/api/v1/login":
                    action = "request.denied"
                else:
                    return
        self._audit_recorded = True
        try:
            log.record(
                request_id=self._request_id,
                actor_id=self._audit_actor_id,
                action=action,
                target=target,
                status=status,
                code=self._audit_code,
                started_at=started_at,
            )
        except Exception:
            # An audit-sink failure never changes the HTTP result and never
            # causes a retry or a recursive audit.  The connection is aborted
            # only when the response itself could not be written.
            pass

    def _route_get(self, head_only=False):
        path = urlsplit(self.path).path

        if path in WEB_BUNDLE:
            self._get_web_file(path, head_only=head_only)
        elif _match_course_file(path) is not None:
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
        elif path == ADMIN_USERS_PATH:
            self._get_admin_users(head_only=head_only)
        elif path == ADMIN_PLAYGROUNDS_PATH:
            self._get_admin_playgrounds(head_only=head_only)
        elif path == PLAYGROUNDS_PATH:
            self._get_playgrounds(head_only=head_only)
        elif path == "/api/v1/me":
            self._get_me(head_only=head_only)
        elif path == CLASSES_PATH:
            self._get_classes(head_only=head_only)
        elif _match_class_releases(path) is not None:
            self._get_class_releases(
                _match_class_releases(path), head_only=head_only
            )
        elif _match_class(path) is not None:
            self._get_class(_match_class(path), head_only=head_only)
        elif _match_class_members(path) is not None:
            self._get_class_members(
                _match_class_members(path), head_only=head_only
            )
        elif path == CATALOG_PATH:
            self._get_catalog(head_only=head_only)
        elif _match_manifest(path) is not None:
            self._get_manifest(*_match_manifest(path), head_only=head_only)
        elif _match_commit_files(path) is not None:
            self._get_commit_file(
                *_match_commit_files(path), head_only=head_only
            )
        elif _match_work_head(path) is not None:
            self._get_work_head(_match_work_head(path), head_only=head_only)
        elif _match_work_history(path) is not None:
            self._get_work_history(
                _match_work_history(path), head_only=head_only
            )
        elif _match_work_manifest(path) is not None:
            self._get_work_manifest(
                *_match_work_manifest(path), head_only=head_only
            )
        elif _match_work_commit_files(path) is not None:
            self._get_work_commit_file(
                *_match_work_commit_files(path), head_only=head_only
            )
        elif path == RELEASES_PATH:
            self._get_releases(head_only=head_only)
        elif _match_release_submissions(path) is not None:
            self._get_release_submissions(
                _match_release_submissions(path), head_only=head_only
            )
        elif path == SUBMISSIONS_PATH:
            self._get_submissions(head_only=head_only)
        elif _match_submission_comments(path) is not None:
            self._get_submission_comments(
                _match_submission_comments(path), head_only=head_only
            )
        elif _match_submission_work_manifest(path) is not None:
            self._get_submission_work_manifest(
                _match_submission_work_manifest(path), head_only=head_only
            )
        elif _match_submission_work_files(path) is not None:
            self._get_submission_work_file(
                *_match_submission_work_files(path), head_only=head_only
            )
        elif _match_submission(path) is not None:
            self._get_submission(
                _match_submission(path), head_only=head_only
            )
        elif _match_release(path) is not None:
            self._get_release(_match_release(path), head_only=head_only)
        elif _allowed_methods(path) is not None:
            self._method_not_allowed(head_only=head_only)
        else:
            self._not_found(head_only=head_only)

    def _route_post(self):
        path = urlsplit(self.path).path

        if path == "/api/v1/register":
            self._post_register()
        elif path == "/api/v1/login":
            self._post_login()
        elif path == "/api/v1/logout":
            self._post_logout()
        elif path == ADMIN_USERS_PATH:
            self._post_admin_user()
        elif path == ADMIN_CLASSES_PATH:
            self._post_admin_class()
        elif path == PLAYGROUNDS_PATH:
            self._post_playground()
        elif _match_playground_commit(path) is not None:
            self._post_playground_commit(_match_playground_commit(path))
        elif _match_work_sync(path) is not None:
            self._post_work_sync(_match_work_sync(path))
        elif _match_work_restore(path) is not None:
            self._post_work_restore(_match_work_restore(path))
        elif _match_class_releases(path) is not None:
            self._post_class_release(_match_class_releases(path))
        elif _match_release_submissions(path) is not None:
            self._post_release_submission(_match_release_submissions(path))
        elif _match_submission_comments(path) is not None:
            self._post_submission_comment(_match_submission_comments(path))
        elif _allowed_methods(path) is not None:
            self._method_not_allowed()
        else:
            self._not_found()

    def _route_patch(self):
        path = urlsplit(self.path).path
        user_id = _match_admin_user_role(path)
        class_id = _match_class_owner(path)
        playground_id = _match_playground(path)
        if user_id is not None:
            self._patch_admin_user_role(user_id)
        elif class_id is not None:
            self._patch_class_owner(class_id)
        elif playground_id is not None:
            self._patch_playground(playground_id)
        elif _allowed_methods(path) is not None:
            self._method_not_allowed()
        else:
            self._not_found()

    def _route_put(self):
        path = urlsplit(self.path).path
        member = _match_class_member(path)
        if member is not None:
            self._put_class_member(*member)
        elif _allowed_methods(path) is not None:
            self._method_not_allowed()
        else:
            self._not_found()

    def _route_delete(self):
        path = urlsplit(self.path).path
        member = _match_class_member(path)
        if member is not None:
            self._delete_class_member(*member)
        elif _allowed_methods(path) is not None:
            self._method_not_allowed()
        else:
            self._not_found()

    def _post_register(self):
        payload = self._read_json()
        if payload is None:
            return

        try:
            with self._database() as connection:
                user = register_student(
                    connection,
                    payload["username"],
                    payload["password"],
                )
        except KeyError:
            self._api_error(400, "invalid_request")
            return
        except (TypeError, ValueError):
            self._api_error(400, "invalid_request")
            return
        except UsernameTakenError:
            self._api_error(409, "username_taken")
            return

        self._respond_json(201, {"user": self._user_payload(user)})

    def _post_login(self):
        payload = self._read_json()
        if payload is None:
            return

        try:
            with self._database() as connection:
                session = login(
                    connection,
                    payload["username"],
                    payload["password"],
                )
        except KeyError:
            self._api_error(400, "invalid_request")
            return
        except InvalidCredentialsError:
            self._api_error(401, "invalid_credentials")
            return

        self._respond_json(
            200,
            {
                "sessionToken": session.token,
                "expiresAt": session.expires_at,
                "user": self._user_payload(session.user),
            },
            extra_headers=(
                (
                    "Set-Cookie",
                    f"{SESSION_COOKIE_NAME}={session.token}; Path=/; "
                    f"Max-Age={SESSION_COOKIE_MAX_AGE}; HttpOnly; Secure; "
                    "SameSite=Strict",
                ),
            ),
        )

    def _get_me(self, head_only=False):
        user = self._authenticate(head_only=head_only)
        if user is None:
            return

        self._respond_json(
            200,
            {"user": self._user_payload(user)},
            head_only=head_only,
        )

    def _get_classes(self, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return

        parsed = self._list_query(pagination.CLASSES_ROUTE, head_only)
        if parsed is None:
            return
        limit, after = parsed

        try:
            with self._database() as connection:
                page = list_classes(
                    connection, actor, limit=limit, after=after
                )
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        self._respond_json(
            200,
            {
                "classes": [
                    self._class_payload(item) for item in page.items
                ],
                "nextCursor": self._next_cursor(
                    pagination.CLASSES_ROUTE, page.next_key
                ),
            },
            head_only=head_only,
        )

    def _get_class(self, class_id, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return

        try:
            with self._database() as connection:
                class_record = get_class(connection, actor, class_id)
        except MissingClassError:
            self._api_error(404, "not_found", head_only=head_only)
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        self._respond_json(
            200,
            {"class": self._class_payload(class_record)},
            head_only=head_only,
        )

    def _get_class_members(self, class_id, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return

        try:
            with self._database() as connection:
                members = list_memberships(connection, actor, class_id)
        except MissingClassError:
            self._api_error(404, "not_found", head_only=head_only)
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        self._respond_json(
            200,
            {"members": [self._membership_payload(item) for item in members]},
            head_only=head_only,
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
        if self._reject_query_or_body(head_only=head_only):
            return
        generation = self._current_generation()
        if generation is None:
            self._api_error(
                503, "generated_unavailable", head_only=head_only
            )
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

    def _get_artifact(self, version, head_only=False):
        if self._reject_query_or_body(head_only=head_only):
            return
        generation = self._current_generation()
        if generation is None:
            self._api_error(
                503, "generated_unavailable", head_only=head_only
            )
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
            self._audit_code = "not_found"
            self._audit_override = ("artifact.read", version)
            self._respond_json(
                404, {"code": "not_found"}, head_only=head_only
            )
            return
        try:
            target = artifacts.verify_artifact(
                self.server.artifacts_path, entry
            )
        except artifacts.ArtifactError:
            self._audit_code = "artifact_unavailable"
            self._audit_override = ("artifact.read", version)
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
        self._send_request_id()
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
        self._audit_status = status

    def _if_none_match(self, etag):
        header = self.headers.get("If-None-Match")
        return header is not None and header.strip() == f'"{etag}"'

    def _respond_not_modified(self, head_only=False):
        self.send_response(304)
        self._send_request_id()
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._headers_sent = True
        self._audit_status = 304

    def _get_web_file(self, path, head_only=False):
        """Serve one file from the installed Web bundle.

        The bundle is optional: an operator who has not copied the Web build
        onto this host still gets the historical placeholder on ``/`` and a
        404 everywhere else, and no read ever escapes ``web_path``.  The
        content hash doubles as the ETag so a reload reuses the bytes the
        browser already has.
        """

        filename, content_type = WEB_BUNDLE[path]
        try:
            target = (self.server.web_path / filename).resolve()
        except OSError:
            target = None
        if target is None or not target.is_file():
            if path == "/":
                self._respond(
                    200, "Pico service is running\n", head_only=head_only
                )
                return
            self._api_error(404, "not_found", head_only=head_only)
            return
        try:
            data = target.read_bytes()
        except OSError:
            self._api_error(404, "not_found", head_only=head_only)
            return
        self._respond_bytes(
            200,
            data,
            content_type=content_type,
            etag=hashlib.sha256(data).hexdigest(),
            cache_control=WEB_BUNDLE_CACHE_CONTROL,
            head_only=head_only,
        )

    def _get_course_file(
        self, playground_id, commit_id, encoded_path, head_only=False
    ):
        actor = self._authenticate_course(head_only=head_only)
        if actor is None:
            return
        path = _decode_content_path(encoded_path)
        if path is None:
            self._deny_content_read(
                playground_id, commit_id, head_only=head_only
            )
            return

        runtime = (
            path in ("pico.css", "pico.svg")
            or path.startswith("libs/")
            or path.startswith("pyodide/")
        )
        if runtime:
            segments = path.split("/")
            if (
                any(not segment or segment in (".", "..") for segment in segments)
                or segments[0] not in ("libs", "pyodide", "pico.css", "pico.svg")
            ):
                self._deny_content_read(
                    playground_id, commit_id, head_only=head_only
                )
                return
            try:
                with self._database() as connection:
                    authorization.authorize_content_read(
                        connection,
                        actor,
                        self.server.repository,
                        playground_id,
                        commit_id,
                    )
                target = (self.server.runtime_path / path).resolve()
                root = self.server.runtime_path.resolve()
                if root not in target.parents or not target.is_file():
                    raise FileNotFoundError
                data = target.read_bytes()
            except authorization.ContentAccessDeniedError:
                self._deny_content_read(
                    playground_id, commit_id, head_only=head_only
                )
                return
            except (FileNotFoundError, OSError, RepositoryError):
                self._deny_content_read(
                    playground_id, commit_id, head_only=head_only
                )
                return
            self._respond_bytes(
                200,
                data,
                content_type=_course_content_type(path),
                etag=hashlib.sha256(data).hexdigest(),
                cache_control="public, max-age=31536000, immutable",
                extra_headers=(
                    (
                        "Content-Security-Policy",
                        "default-src 'none'; script-src 'self'; "
                        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                        "connect-src 'self'",
                    ),
                ),
                head_only=head_only,
            )
            return

        try:
            with self._database() as connection:
                content = manifests.read_commit_file(
                    connection,
                    actor,
                    self.server.repository,
                    playground_id,
                    commit_id,
                    path,
                )
        except (authorization.ContentAccessDeniedError, manifests.ManifestError):
            self._deny_content_read(
                playground_id, commit_id, head_only=head_only
            )
            return
        except RepositoryError:
            self._api_error(500, "internal_error", head_only=head_only)
            return
        self._respond_bytes(
            200,
            content.data,
            content_type=_course_content_type(path),
            etag=content.sha256,
            cache_control="public, max-age=31536000, immutable",
            extra_headers=(
                (
                    "Content-Security-Policy",
                    "default-src 'none'; script-src 'self'; "
                    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                    "connect-src 'self'",
                ),
            ),
            head_only=head_only,
        )

    def _get_catalog(self, head_only=False):
        actor = self._optional_actor(head_only=head_only)
        if actor is _AUTH_FAILED:
            return

        parsed = self._list_query(pagination.CATALOG_ROUTE, head_only)
        if parsed is None:
            return
        limit, after = parsed

        try:
            with self._database() as connection:
                page = self._build_catalog_page(
                    connection, actor, limit, after
                )
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return
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

    def _build_catalog_page(self, connection, actor, limit, after):
        """Return one bounded, deduplicated catalog page.

        The public-current-head stream and the visible Release-pinned stream
        are each read as bounded, ordered batches, merged, and deduplicated by
        the exact ``(playgroundId, commitId)`` pair so a commit reachable both
        publicly and through a Release appears once with all its Release ids.
        """

        after_playground = after[0] if after is not None else None
        after_commit = after[1] if after is not None else None

        public_keys = []
        cursor = after_playground
        while len(public_keys) < limit + 1:
            parameters = [PUBLIC_POLICY]
            where = "WHERE policy = ? AND archived_at IS NULL"
            if cursor is not None:
                where += " AND id > ?"
                parameters.append(cursor)
            parameters.append(limit + 1 - len(public_keys))
            rows = connection.execute(
                "SELECT id FROM playgrounds "
                f"{where} ORDER BY id LIMIT ?",
                parameters,
            ).fetchall()
            if not rows:
                break
            for row in rows:
                cursor = row["id"]
                if not valid_playground_id(row["id"]):
                    raise RepositoryError(
                        "stored playground identifier is invalid"
                    )
                commit_id = manifests.current_playground_commit(
                    self.server.repository, row["id"]
                )
                if commit_id is None:
                    continue
                key = (row["id"], commit_id)
                if after is not None and key <= (
                    after_playground,
                    after_commit,
                ):
                    continue
                public_keys.append(key)
                if len(public_keys) >= limit + 1:
                    break

        entries = {}
        for key in public_keys:
            entries.setdefault(key, [])
        for key, release_id in self._release_catalog_rows(
            connection, actor, limit, after
        ):
            entries.setdefault(key, []).append(release_id)

        ordered = sorted(entries)[: limit + 1]
        next_key = list(ordered[limit - 1]) if len(ordered) > limit else None
        items = tuple(
            {
                "playgroundId": key[0],
                "commitId": key[1],
                "releaseIds": sorted(set(entries[key])),
            }
            for key in ordered[:limit]
        )
        return pagination.Page(items=items, next_key=next_key)

    def _release_catalog_rows(self, connection, actor, limit, after):
        """Return bounded ``((playground_id, commit_id), release_id)`` rows."""

        if actor is None:
            return []
        joins = ""
        conditions = []
        parameters = []
        if actor.role == "teacher":
            joins = " JOIN classes ON classes.id = releases.class_id"
            conditions.append("classes.teacher_id = ?")
            parameters.append(actor.id)
        elif actor.role == "student":
            joins = (
                " JOIN class_memberships"
                " ON class_memberships.class_id = releases.class_id"
            )
            conditions.append("class_memberships.student_id = ?")
            parameters.append(actor.id)
            conditions.append("releases.open_at <= ?")
            parameters.append(
                datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                )
            )
        elif actor.role == "admin":
            pass
        else:
            return []

        if after is not None:
            conditions.append(
                "(releases.playground_id, releases.playground_commit_id)"
                " > (?, ?)"
            )
            parameters.extend([after[0], after[1]])
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(limit + 1)
        rows = connection.execute(
            "SELECT releases.playground_id AS playground_id,"
            " releases.playground_commit_id AS commit_id,"
            " releases.id AS release_id"
            " FROM releases" + joins + where
            + " ORDER BY releases.playground_id,"
            " releases.playground_commit_id, releases.id LIMIT ?",
            parameters,
        ).fetchall()
        return [
            ((row["playground_id"], row["commit_id"]), row["release_id"])
            for row in rows
        ]

    def _get_manifest(self, playground_id, commit_id, head_only=False):
        actor = self._optional_actor(head_only=head_only)
        if actor is _AUTH_FAILED:
            return

        try:
            with self._database() as connection:
                manifest = manifests.generate_manifest(
                    connection,
                    actor,
                    self.server.repository,
                    playground_id,
                    commit_id,
                )
        except authorization.ContentAccessDeniedError:
            self._deny_content_read(
                playground_id,
                commit_id,
                code="content_denied",
                head_only=head_only,
            )
            return
        except manifests.ManifestError:
            self._deny_content_read(
                playground_id,
                commit_id,
                code="not_found",
                head_only=head_only,
            )
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
        actor = self._optional_actor(head_only=head_only)
        if actor is _AUTH_FAILED:
            return

        path = _decode_content_path(encoded_path)
        if path is None:
            self._api_error(404, "not_found", head_only=head_only)
            return

        try:
            with self._database() as connection:
                content = manifests.read_commit_file(
                    connection,
                    actor,
                    self.server.repository,
                    playground_id,
                    commit_id,
                    path,
                )
        except authorization.ContentAccessDeniedError:
            self._deny_content_read(
                playground_id,
                commit_id,
                code="content_denied",
                head_only=head_only,
            )
            return
        except manifests.ManifestError:
            self._deny_content_read(
                playground_id,
                commit_id,
                code="not_found",
                head_only=head_only,
            )
            return
        except RepositoryError:
            self._api_error(500, "internal_error", head_only=head_only)
            return

        self._respond_file(content.data, content.sha256, head_only=head_only)

    def _get_work_head(self, playground_id, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return
        if actor.role != "student":
            self._deny_hidden_read("work.head", playground_id, head_only)
            return

        try:
            info = works.current_work_head_info(
                self.server.repository, playground_id, actor.id
            )
        except works.WorkError:
            self._api_error(500, "internal_error", head_only=head_only)
            return
        except RepositoryError:
            self._api_error(500, "internal_error", head_only=head_only)
            return
        if info is None:
            self._deny_hidden_read("work.head", playground_id, head_only)
            return

        head, pinned = info
        self._respond_json(
            200,
            {
                "work": {
                    "playgroundId": playground_id,
                    "workCommitId": head,
                    "playgroundCommitId": pinned,
                }
            },
            head_only=head_only,
        )

    def _get_work_history(self, playground_id, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return
        if actor.role != "student":
            self._deny_hidden_read("work.history", playground_id, head_only)
            return

        parsed = self._parse_work_history_query()
        if parsed is None:
            self._api_error(400, "invalid_request", head_only=head_only)
            return
        limit, cursor = parsed

        try:
            history = works.list_work_history(
                self.server.repository,
                playground_id,
                actor.id,
                cursor=cursor,
                limit=limit,
            )
        except works.WorkValidationError:
            self._api_error(400, "invalid_request", head_only=head_only)
            return
        except RepositoryError:
            self._api_error(500, "internal_error", head_only=head_only)
            return

        self._respond_json(
            200,
            {
                "commits": [
                    {
                        "workCommitId": entry.work_commit_id,
                        "playgroundCommitId": entry.playground_commit_id,
                        "parentWorkCommitId": entry.parent_work_commit_id,
                        "createdAt": entry.created_at,
                    }
                    for entry in history.entries
                ],
                "nextCursor": history.next_cursor,
            },
            head_only=head_only,
        )

    def _parse_work_history_query(self):
        pairs = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        if set(pairs) - {"limit", "cursor"}:
            return None
        for values in pairs.values():
            if len(values) != 1:
                return None

        limit = 50
        if "limit" in pairs:
            raw = pairs["limit"][0]
            if not raw.isascii() or not raw.isdigit():
                return None
            limit = int(raw)
            if not 1 <= limit <= 100:
                return None

        cursor = None
        if "cursor" in pairs:
            cursor = pairs["cursor"][0]
            if not cursor:
                return None
        return limit, cursor

    def _get_work_manifest(self, playground_id, commit_id, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return
        if actor.role != "student":
            self._api_error(404, "not_found", head_only=head_only)
            return

        try:
            with self._database() as connection:
                manifest = work_content.get_work_manifest(
                    connection,
                    actor,
                    self.server.repository,
                    actor.id,
                    playground_id,
                    commit_id,
                )
        except authorization.ContentAccessDeniedError:
            self._deny_work_read(playground_id, commit_id, head_only=head_only)
            return
        except work_content.WorkContentError as error:
            self._handle_work_content_error(
                error, playground_id, commit_id, head_only
            )
            return
        except RepositoryError:
            self._api_error(500, "internal_error", head_only=head_only)
            return

        self._respond_json(
            200,
            {
                "manifest": {
                    "playgroundId": manifest.playground_id,
                    "workCommitId": manifest.work_commit_id,
                    "playgroundCommitId": manifest.playground_commit_id,
                    "files": [
                        {
                            "path": item.path,
                            "size": item.size,
                            "sha256": item.sha256,
                        }
                        for item in manifest.files
                    ],
                }
            },
            head_only=head_only,
        )

    def _get_work_commit_file(
        self, playground_id, commit_id, encoded_path, head_only=False
    ):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return
        if actor.role != "student":
            self._api_error(404, "not_found", head_only=head_only)
            return

        path = _decode_content_path(encoded_path)
        if path is None:
            self._deny_work_read(playground_id, commit_id, head_only=head_only)
            return

        try:
            with self._database() as connection:
                content = work_content.read_work_file(
                    connection,
                    actor,
                    self.server.repository,
                    actor.id,
                    playground_id,
                    commit_id,
                    path,
                )
        except authorization.ContentAccessDeniedError:
            self._deny_work_read(playground_id, commit_id, head_only=head_only)
            return
        except work_content.WorkContentError as error:
            self._handle_work_content_error(
                error, playground_id, commit_id, head_only
            )
            return
        except RepositoryError:
            self._api_error(500, "internal_error", head_only=head_only)
            return

        self._respond_file(content.data, content.sha256, head_only=head_only)

    def _handle_work_content_error(
        self, error, playground_id, commit_id, head_only
    ):
        if error.reason in (
            work_content.REASON_PATH_NOT_FOUND,
            work_content.REASON_PATH_NOT_ALLOWED,
        ):
            self._deny_work_read(playground_id, commit_id, head_only=head_only)
            return
        self._api_error(500, "internal_error", head_only=head_only)

    def _deny_work_read(self, playground_id, commit_id, head_only=False):
        self._audit_code = "not_found_or_denied"
        self._audit_override = (
            "work.read",
            f"{playground_id}/{commit_id}",
        )
        self._respond_json(404, {"code": "not_found"}, head_only=head_only)

    def _get_admin_users(self, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return
        try:
            limit, after = pagination.parse_pagination(
                urlsplit(self.path).query, pagination.ADMIN_USERS_ROUTE
            )
        except pagination.PaginationError:
            self._api_error(400, "invalid_request", head_only=head_only)
            return
        try:
            with self._database() as connection:
                page = list_users(connection, actor, limit=limit, after=after)
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return
        except StoredUserError:
            self._api_error(500, "internal_error", head_only=head_only)
            return
        self._respond_json(
            200,
            {
                "users": [
                    {
                        "userId": item.id,
                        "role": item.role,
                        "createdAt": item.created_at,
                    }
                    for item in page.items
                ],
                "nextCursor": (
                    pagination.encode_cursor(
                        pagination.ADMIN_USERS_ROUTE, page.next_key
                    )
                    if page.next_key is not None
                    else None
                ),
            },
            head_only=head_only,
        )

    def _get_admin_playgrounds(self, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return
        try:
            limit, after = pagination.parse_pagination(
                urlsplit(self.path).query,
                pagination.ADMIN_PLAYGROUNDS_ROUTE,
            )
        except pagination.PaginationError:
            self._api_error(400, "invalid_request", head_only=head_only)
            return
        try:
            with self._database() as connection:
                page = list_playgrounds(
                    connection, actor, limit=limit, after=after
                )
                items = [
                    {
                        "playgroundId": item.id,
                        "archived": item.archived,
                        "headCommitId": manifests.current_playground_commit(
                            self.server.repository, item.id
                        ),
                    }
                    for item in page.items
                ]
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return
        except (StoredPlaygroundError, RepositoryError):
            self._api_error(500, "internal_error", head_only=head_only)
            return
        self._respond_json(
            200,
            {
                "playgrounds": items,
                "nextCursor": (
                    pagination.encode_cursor(
                        pagination.ADMIN_PLAYGROUNDS_ROUTE, page.next_key
                    )
                    if page.next_key is not None
                    else None
                ),
            },
            head_only=head_only,
        )

    def _get_playgrounds(self, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return
        try:
            limit, after = pagination.parse_pagination(
                urlsplit(self.path).query,
                pagination.PLAYGROUNDS_ROUTE,
            )
            with self._database() as connection:
                page = list_releasable_playground_ids(
                    connection, actor, limit=limit, after=after
                )
            items = []
            for playground_id in page.items:
                commit_id = manifests.current_playground_commit(
                    self.server.repository, playground_id
                )
                if commit_id is None:
                    continue
                metadata = manifests.course_metadata(
                    self.server.repository, commit_id
                )
                item = {
                    "id": playground_id,
                    "commitId": commit_id,
                    "requiresSubmission": metadata.requires_submission,
                }
                if metadata.title is not None:
                    item["title"] = metadata.title
                if metadata.summary is not None:
                    item["summary"] = metadata.summary
                items.append(item)
        except (pagination.PaginationError, ValueError):
            self._api_error(400, "invalid_request", head_only=head_only)
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return
        except (StoredPlaygroundError, RepositoryError):
            self._api_error(500, "internal_error", head_only=head_only)
            return
        self._respond_json(
            200,
            {
                "playgrounds": items,
                "nextCursor": (
                    pagination.encode_cursor(
                        pagination.PLAYGROUNDS_ROUTE, page.next_key
                    )
                    if page.next_key is not None
                    else None
                ),
            },
            head_only=head_only,
        )

    def _post_admin_user(self):
        actor = self._authenticate()
        if actor is None:
            return

        payload = self._read_json()
        if payload is None:
            return

        try:
            with self._database() as connection:
                user = create_user(
                    connection,
                    actor,
                    payload["username"],
                    payload["password"],
                    payload["role"],
                )
        except KeyError:
            self._api_error(400, "invalid_request")
            return
        except (TypeError, ValueError):
            self._api_error(400, "invalid_request")
            return
        except InvalidRoleError:
            self._api_error(400, "invalid_request")
            return
        except UsernameTakenError:
            self._api_error(409, "username_taken")
            return
        except RoleConflictError:
            self._api_error(409, "role_conflict")
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden")
            return

        self._respond_json(201, {"user": self._user_payload(user)})

    def _post_admin_class(self):
        actor = self._authenticate()
        if actor is None:
            return

        payload = self._read_json()
        if payload is None:
            return

        try:
            with self._database() as connection:
                class_record = create_class(
                    connection,
                    actor,
                    payload["name"],
                    payload["teacherId"],
                )
        except KeyError:
            self._api_error(400, "invalid_request")
            return
        except (TypeError, ValueError):
            self._api_error(400, "invalid_request")
            return
        except InvalidClassNameError:
            self._api_error(400, "invalid_request")
            return
        except MissingUserError:
            self._api_error(404, "not_found")
            return
        except RoleMismatchError:
            self._api_error(400, "invalid_request")
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden")
            return

        self._respond_json(201, {"class": self._class_payload(class_record)})

    def _patch_admin_user_role(self, user_id):
        actor = self._authenticate()
        if actor is None:
            return

        payload = self._read_json()
        if payload is None:
            return

        try:
            with self._database() as connection:
                user = change_user_role(
                    connection,
                    actor,
                    user_id,
                    payload["role"],
                )
        except KeyError:
            self._api_error(400, "invalid_request")
            return
        except (TypeError, ValueError):
            self._api_error(400, "invalid_request")
            return
        except InvalidRoleError:
            self._api_error(400, "invalid_request")
            return
        except MissingUserError:
            self._api_error(404, "not_found")
            return
        except RoleConflictError:
            self._api_error(409, "role_conflict")
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden")
            return

        self._respond_json(200, {"user": self._user_payload(user)})

    def _patch_class_owner(self, class_id):
        actor = self._authenticate()
        if actor is None:
            return

        payload = self._read_json()
        if payload is None:
            return

        try:
            with self._database() as connection:
                class_record = change_class_owner(
                    connection,
                    actor,
                    class_id,
                    payload["teacherId"],
                )
        except KeyError:
            self._api_error(400, "invalid_request")
            return
        except (TypeError, ValueError):
            self._api_error(400, "invalid_request")
            return
        except MissingClassError:
            self._api_error(404, "not_found")
            return
        except MissingUserError:
            self._api_error(404, "not_found")
            return
        except RoleMismatchError:
            self._api_error(400, "invalid_request")
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden")
            return

        self._respond_json(200, {"class": self._class_payload(class_record)})

    def _put_class_member(self, class_id, student_id):
        actor = self._authenticate()
        if actor is None:
            return

        try:
            with self._database() as connection:
                membership = assign_student(
                    connection,
                    actor,
                    student_id,
                    class_id,
                )
        except MissingClassError:
            self._api_error(404, "not_found")
            return
        except MissingUserError:
            self._api_error(404, "not_found")
            return
        except RoleMismatchError:
            self._api_error(400, "invalid_request")
            return
        except ArchivedClassError:
            self._api_error(409, "class_archived")
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden")
            return

        self._respond_json(
            200,
            {"membership": self._membership_payload(membership)},
        )

    def _delete_class_member(self, class_id, student_id):
        actor = self._authenticate()
        if actor is None:
            return

        try:
            with self._database() as connection:
                remove_membership(connection, actor, class_id, student_id)
        except MissingClassError:
            self._api_error(404, "not_found")
            return
        except MissingUserError:
            self._api_error(404, "not_found")
            return
        except MembershipNotFoundError:
            self._api_error(404, "not_found")
            return
        except RoleMismatchError:
            self._api_error(400, "invalid_request")
            return
        except ArchivedClassError:
            self._api_error(409, "class_archived")
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden")
            return

        self._respond(204, "")

    def _post_playground(self):
        actor = self._authenticate()
        if actor is None:
            return

        payload = self._read_json()
        if payload is None:
            return

        try:
            with self._database() as connection:
                playground = create_playground(
                    connection,
                    actor,
                    payload["id"],
                )
        except KeyError:
            self._api_error(400, "invalid_request")
            return
        except InvalidRequestError:
            self._api_error(400, "invalid_request")
            return
        except PlaygroundConflictError:
            self._api_error(409, "conflict")
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden")
            return

        self._respond_json(
            201,
            {"playground": self._playground_payload(playground)},
        )

    def _post_playground_commit(self, playground_id):
        actor = self._authenticate()
        if actor is None:
            return

        if self.headers.get_content_type() != "application/zip":
            self._api_error(415, "unsupported_media_type")
            return

        request_key = self.headers.get("Idempotency-Key")
        if request_key is None or not request_key.strip():
            self._api_error(400, "invalid_request")
            return
        if len(request_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            self._api_error(400, "invalid_request")
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
                    actor.id,
                    f"POST /api/v1/playgrounds/{playground_id}/commits",
                    request_key,
                    _bundle_request(playground_id, bundle),
                    lambda connection: self._publish_bundle(
                        connection, actor, playground_id, bundle
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
        except ForbiddenActionError:
            self._api_error(403, "forbidden")
            return
        except RepositoryError:
            self._api_error(500, "internal_error")
            return

        if not self._refresh_generated():
            self._audit_code = "generated_rebuild_failed"
            self._audit_override = ("generated.rebuild", None)
        self._respond_json(status, body)

    def _post_work_sync(self, playground_id):
        actor = self._authenticate()
        if actor is None:
            return

        payload = self._read_json(max_bytes=works.MAX_SYNC_JSON_BODY_BYTES)
        if payload is None:
            return

        try:
            request = works.decode_work_sync_request(payload)
        except works.WorkValidationError as error:
            self._api_error(
                400, "invalid_request", details={"reason": error.reason}
            )
            return

        try:
            with self._database() as connection:
                result = works.create_work_commit(
                    connection,
                    actor,
                    self.server.repository,
                    playground_id,
                    request.playground_commit_id,
                    request.files,
                    base_commit_id=request.base_commit_id,
                )
        except works.WorkValidationError as error:
            self._api_error(
                400, "invalid_request", details={"reason": error.reason}
            )
            return
        except WorkHeadChangedError:
            # The caller's own canonical HEAD is reported; ownership was
            # already established, so no other Student's HEAD is exposed.
            current_head = works.current_work_head(
                self.server.repository, playground_id, actor.id
            )
            self._audit_code = "work_head_changed"
            self._respond_json(
                409,
                {
                    "code": "work_head_changed",
                    "currentHeadCommitId": current_head,
                },
            )
            return
        except authorization.ContentAccessDeniedError:
            self._api_error(403, "forbidden")
            return
        except RepositoryError:
            self._api_error(500, "internal_error")
            return

        status = 201 if result.base_commit_id is None else 200
        self._respond_json(
            status,
            {
                "workCommitId": result.commit_id,
                "playgroundCommitId": result.playground_commit_id,
            },
        )

    def _post_work_restore(self, playground_id):
        actor = self._authenticate()
        if actor is None:
            return

        payload = self._read_json()
        if payload is None:
            return

        try:
            request = works.decode_work_restore_request(payload)
        except works.WorkValidationError as error:
            self._api_error(
                400, "invalid_request", details={"reason": error.reason}
            )
            return

        try:
            with self._database() as connection:
                result = works.restore_work_commit(
                    connection,
                    actor,
                    self.server.repository,
                    playground_id,
                    request.base_commit_id,
                    request.source_commit_id,
                )
        except works.WorkValidationError as error:
            self._api_error(
                400, "invalid_request", details={"reason": error.reason}
            )
            return
        except WorkHeadChangedError:
            # An appended restore never rewinds the ref.  The caller's own
            # canonical HEAD is reported; ownership was already established,
            # so no other Student's HEAD is exposed.
            current_head = works.current_work_head(
                self.server.repository, playground_id, actor.id
            )
            self._audit_code = "work_head_changed"
            self._respond_json(
                409,
                {
                    "code": "work_head_changed",
                    "currentHeadCommitId": current_head,
                },
            )
            return
        except authorization.ContentAccessDeniedError:
            self._api_error(403, "forbidden")
            return
        except RepositoryError:
            self._api_error(500, "internal_error")
            return

        self._respond_json(
            201,
            {
                "workCommitId": result.commit_id,
                "playgroundCommitId": result.playground_commit_id,
            },
        )

    def _publish_bundle(self, connection, actor, playground_id, bundle):
        result = publish_playground(
            connection,
            actor,
            playground_id,
            self.server.repository,
            bundle,
            _commit_metadata(actor),
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

    def _get_releases(self, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return

        parsed = self._list_query(pagination.RELEASES_ROUTE, head_only)
        if parsed is None:
            return
        limit, after = parsed

        try:
            with self._database() as connection:
                page = list_releases(
                    connection, actor, limit=limit, after=after
                )
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        self._respond_json(
            200,
            {
                "releases": [
                    release_to_mapping(item) for item in page.items
                ],
                "nextCursor": self._next_cursor(
                    pagination.RELEASES_ROUTE, page.next_key
                ),
            },
            head_only=head_only,
        )

    def _get_class_releases(self, class_id, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return

        route = pagination.scoped_route(pagination.RELEASES_ROUTE, class_id)
        parsed = self._list_query(route, head_only)
        if parsed is None:
            return
        limit, after = parsed

        try:
            with self._database() as connection:
                page = list_releases(
                    connection,
                    actor,
                    class_id=class_id,
                    limit=limit,
                    after=after,
                )
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        self._respond_json(
            200,
            {
                "releases": [
                    release_to_mapping(item) for item in page.items
                ],
                "nextCursor": self._next_cursor(route, page.next_key),
            },
            head_only=head_only,
        )

    def _get_release(self, release_id, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return

        try:
            with self._database() as connection:
                release = get_release(connection, actor, release_id)
        except MissingReleaseError:
            self._deny_hidden_read("release.read", release_id, head_only)
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        self._respond_json(
            200,
            {"release": release_to_mapping(release)},
            head_only=head_only,
        )

    def _get_submissions(self, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return

        parsed = self._list_query(pagination.SUBMISSIONS_ROUTE, head_only)
        if parsed is None:
            return
        limit, after = parsed

        try:
            with self._database() as connection:
                page = list_submissions(
                    connection, actor, limit=limit, after=after
                )
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
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

    def _get_release_submissions(self, release_id, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return

        route = pagination.scoped_route(
            pagination.SUBMISSIONS_ROUTE, release_id
        )
        parsed = self._list_query(route, head_only)
        if parsed is None:
            return
        limit, after = parsed

        try:
            with self._database() as connection:
                release = get_release(connection, actor, release_id)
                page = list_submissions(
                    connection,
                    actor,
                    release_id=release.id,
                    limit=limit,
                    after=after,
                )
        except MissingReleaseError:
            self._deny_hidden_read("release.read", release_id, head_only)
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        self._respond_json(
            200,
            {
                "submissions": [
                    submission_to_mapping(item) for item in page.items
                ],
                "nextCursor": self._next_cursor(route, page.next_key),
            },
            head_only=head_only,
        )

    def _get_submission(self, submission_id, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return

        try:
            with self._database() as connection:
                submission = get_submission(connection, actor, submission_id)
        except MissingSubmissionError:
            self._deny_hidden_read("submission.read", submission_id, head_only)
            return
        except InvalidSubmissionRequestError:
            self._api_error(400, "invalid_request", head_only=head_only)
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        self._respond_json(
            200,
            {"submission": submission_to_mapping(submission)},
            head_only=head_only,
        )

    def _get_submission_comments(self, submission_id, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return

        route = pagination.scoped_route("submission.comments", submission_id)
        parsed = self._list_query(route, head_only)
        if parsed is None:
            return
        limit, after = parsed

        try:
            with self._database() as connection:
                page = list_comments(
                    connection,
                    actor,
                    submission_id,
                    limit=limit,
                    after=after,
                )
        except MissingSubmissionError:
            self._deny_hidden_read("comment.read", submission_id, head_only)
            return
        except InvalidCommentRequestError:
            self._api_error(400, "invalid_request", head_only=head_only)
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        self._respond_json(
            200,
            {
                "comments": [
                    comment_to_mapping(item) for item in page.items
                ],
                "nextCursor": self._next_cursor(route, page.next_key),
            },
            head_only=head_only,
        )

    def _get_submission_work_manifest(self, submission_id, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return
        if self._reject_query_or_body(head_only=head_only):
            return

        try:
            with self._database() as connection:
                review = review_submission(
                    connection, actor, self.server.repository, submission_id
                )
                manifest = work_content.get_work_manifest(
                    connection,
                    actor,
                    self.server.repository,
                    review.student_id,
                    review.playground_id,
                    review.work_commit_id,
                )
        except (MissingSubmissionError, InvalidSubmissionRequestError):
            self._deny_submission_work(submission_id, head_only=head_only)
            return
        except (
            SubmissionIntegrityError,
            authorization.ContentAccessDeniedError,
            work_content.WorkContentError,
            RepositoryError,
        ):
            self._api_error(500, "internal_error", head_only=head_only)
            return

        self._audit_override = ("submission.work.read", submission_id)
        self._respond_json(
            200,
            {
                "manifest": {
                    "submissionId": review.submission_id,
                    "playgroundId": manifest.playground_id,
                    "workCommitId": manifest.work_commit_id,
                    "playgroundCommitId": manifest.playground_commit_id,
                    "files": [
                        {
                            "path": item.path,
                            "size": item.size,
                            "sha256": item.sha256,
                        }
                        for item in manifest.files
                    ],
                }
            },
            head_only=head_only,
        )

    def _get_submission_work_file(
        self, submission_id, encoded_path, head_only=False
    ):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return
        if self._reject_query_or_body(head_only=head_only):
            return

        path = _decode_content_path(encoded_path)
        if path is None:
            self._deny_submission_work(submission_id, head_only=head_only)
            return

        try:
            with self._database() as connection:
                review = review_submission(
                    connection, actor, self.server.repository, submission_id
                )
                content = work_content.read_work_file(
                    connection,
                    actor,
                    self.server.repository,
                    review.student_id,
                    review.playground_id,
                    review.work_commit_id,
                    path,
                )
        except (MissingSubmissionError, InvalidSubmissionRequestError):
            self._deny_submission_work(submission_id, head_only=head_only)
            return
        except work_content.WorkContentError as error:
            if error.reason in (
                work_content.REASON_PATH_NOT_FOUND,
                work_content.REASON_PATH_NOT_ALLOWED,
            ):
                self._deny_submission_work(
                    submission_id, head_only=head_only
                )
            else:
                self._api_error(500, "internal_error", head_only=head_only)
            return
        except (
            SubmissionIntegrityError,
            authorization.ContentAccessDeniedError,
            RepositoryError,
        ):
            self._api_error(500, "internal_error", head_only=head_only)
            return

        self._audit_override = ("submission.work.read", submission_id)
        self._respond_file(content.data, content.sha256, head_only=head_only)

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

    def _deny_submission_work(self, submission_id, head_only=False):
        """Answer a missing or denied Submission review with one hidden 404."""

        self._audit_code = "not_found_or_denied"
        self._audit_override = ("submission.work.read", submission_id)
        self._respond_json(404, {"code": "not_found"}, head_only=head_only)

    def _post_submission_comment(self, submission_id):
        actor = self._authenticate()
        if actor is None:
            return

        request_key = self.headers.get("Idempotency-Key")
        if request_key is None or not request_key.strip():
            self._api_error(400, "invalid_request")
            return
        if len(request_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            self._api_error(400, "invalid_request")
            return

        payload = self._read_json(max_bytes=MAX_COMMENT_JSON_BODY_BYTES)
        if payload is None:
            return

        # Author and source are server-owned.  A caller that tries to select
        # either is rejected instead of being silently ignored.
        if "authorUserId" in payload or "source" in payload:
            self._api_error(400, "invalid_request")
            return

        try:
            with self._database() as connection:
                status, body = create_comment(
                    connection,
                    actor,
                    submission_id,
                    payload["body"],
                    request_key,
                )
        except KeyError:
            self._api_error(400, "invalid_request")
            return
        except InvalidCommentRequestError:
            self._api_error(400, "invalid_request")
            return
        except MissingSubmissionError:
            self._api_error(404, "not_found")
            return
        except IdempotencyConflict:
            self._api_error(409, "idempotency_conflict")
            return
        except IdempotencyRequestError:
            self._api_error(400, "invalid_request")
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden")
            return

        self._respond_json(status, body)

    def _post_class_release(self, class_id):
        actor = self._authenticate()
        if actor is None:
            return

        request_key = self.headers.get("Idempotency-Key")
        if request_key is None or not request_key.strip():
            self._api_error(400, "invalid_request")
            return
        if len(request_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            self._api_error(400, "invalid_request")
            return

        payload = self._read_json()
        if payload is None:
            return

        try:
            with self._database() as connection:
                status, body = create_release(
                    connection,
                    actor,
                    self.server.repository,
                    class_id,
                    payload["playgroundId"],
                    payload["playgroundCommitId"],
                    payload["openAt"],
                    payload["requiresSubmission"],
                    request_key,
                    due_at=payload.get("dueAt"),
                )
        except KeyError:
            self._api_error(400, "invalid_request")
            return
        except InvalidReleaseRequestError:
            self._api_error(400, "invalid_request")
            return
        except MissingClassError:
            self._api_error(404, "not_found")
            return
        except MissingPlaygroundError:
            self._api_error(404, "not_found")
            return
        except UnknownPlaygroundCommitError:
            self._api_error(404, "not_found")
            return
        except IdempotencyConflict:
            self._api_error(409, "idempotency_conflict")
            return
        except IdempotencyRequestError:
            self._api_error(400, "invalid_request")
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden")
            return
        except RepositoryError:
            self._api_error(500, "internal_error")
            return

        self._respond_json(status, body)

    def _post_release_submission(self, release_id):
        actor = self._authenticate()
        if actor is None:
            return

        request_key = self.headers.get("Idempotency-Key")
        if request_key is None or not request_key.strip():
            self._api_error(400, "invalid_request")
            return
        if len(request_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            self._api_error(400, "invalid_request")
            return

        payload = self._read_json()
        if payload is None:
            return

        try:
            with self._database() as connection:
                status, body = create_submission(
                    connection,
                    actor,
                    self.server.repository,
                    release_id,
                    payload["workCommitId"],
                    request_key,
                )
        except KeyError:
            self._api_error(400, "invalid_request")
            return
        except InvalidSubmissionRequestError:
            self._api_error(400, "invalid_request")
            return
        except UnknownReleaseError:
            self._api_error(404, "not_found")
            return
        except SubmissionNotAllowedError:
            self._api_error(409, "submission_closed")
            return
        except UnknownWorkCommitError:
            self._api_error(409, "playground_version_mismatch")
            return
        except IdempotencyConflict:
            self._api_error(409, "idempotency_conflict")
            return
        except IdempotencyRequestError:
            self._api_error(400, "invalid_request")
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden")
            return
        except RepositoryError:
            self._api_error(500, "internal_error")
            return

        self._respond_json(status, body)

    def _patch_playground(self, playground_id):
        actor = self._authenticate()
        if actor is None:
            return

        payload = self._read_json()
        if payload is None:
            return

        try:
            with self._database() as connection:
                playground = update_playground(
                    connection,
                    actor,
                    playground_id,
                    payload,
                )
        except InvalidRequestError:
            self._api_error(400, "invalid_request")
            return
        except InvalidPolicyError:
            self._api_error(400, "invalid_request")
            return
        except MissingPlaygroundError:
            self._api_error(404, "not_found")
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden")
            return

        if not self._refresh_generated():
            self._audit_code = "generated_rebuild_failed"
            self._audit_override = ("generated.rebuild", None)
        self._respond_json(
            200,
            {"playground": self._playground_payload(playground)},
        )

    def _authenticate(self, head_only=False):
        token = self._bearer_token()
        if token is None:
            self._api_error(401, "invalid_session", head_only=head_only)
            return None

        try:
            with self._database() as connection:
                actor = authenticate_session(connection, token)
        except InvalidSessionError:
            self._api_error(401, "invalid_session", head_only=head_only)
            return None
        self._audit_actor_id = actor.id
        return actor

    def _authenticate_course(self, head_only=False):
        """Authenticate course GETs with Bearer or the same-origin session cookie."""

        token = self._bearer_token()
        if token is None and self.headers.get("Authorization") is None:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except (CookieError, ValueError):
                cookie = SimpleCookie()
            morsel = cookie.get(SESSION_COOKIE_NAME)
            token = None if morsel is None else morsel.value
        if token is None:
            self._api_error(401, "invalid_session", head_only=head_only)
            return None
        try:
            with self._database() as connection:
                actor = authenticate_session(connection, token)
        except InvalidSessionError:
            self._api_error(401, "invalid_session", head_only=head_only)
            return None
        self._audit_actor_id = actor.id
        return actor

    def _optional_actor(self, head_only=False):
        """Resolve an optional session for a public-capable read route.

        Returns ``None`` when no ``Authorization`` header was supplied, the
        authenticated account when a valid session was supplied, and the
        :data:`_AUTH_FAILED` sentinel after answering 401 when a header was
        supplied but carried no valid session.
        """

        if self.headers.get("Authorization") is None:
            return None

        token = self._bearer_token()
        if token is None:
            self._api_error(401, "invalid_session", head_only=head_only)
            return _AUTH_FAILED

        try:
            with self._database() as connection:
                actor = authenticate_session(connection, token)
        except InvalidSessionError:
            self._api_error(401, "invalid_session", head_only=head_only)
            return _AUTH_FAILED
        self._audit_actor_id = actor.id
        return actor

    def _post_logout(self):
        token = self._bearer_token()
        if token is None:
            self._api_error(401, "invalid_session")
            return

        with self._database() as connection:
            try:
                actor = authenticate_session(connection, token)
            except InvalidSessionError:
                actor = None
            if actor is not None:
                self._audit_actor_id = actor.id
            revoke_session(connection, token)
        self._respond(
            204,
            "",
            extra_headers=(
                (
                    "Set-Cookie",
                    f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; "
                    "Secure; SameSite=Strict",
                ),
            ),
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
    def _user_payload(user):
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "createdAt": user.created_at,
        }

    @staticmethod
    def _class_payload(class_record):
        return {
            "id": class_record.id,
            "name": class_record.name,
            "teacherId": class_record.teacher_id,
            "createdAt": class_record.created_at,
            "archivedAt": class_record.archived_at,
        }

    @staticmethod
    def _membership_payload(membership):
        return {
            "studentId": membership.student_id,
            "classId": membership.class_id,
            "username": membership.username,
            "joinedAt": membership.joined_at,
        }

    @staticmethod
    def _playground_payload(playground):
        return {
            "id": playground.id,
            "creatorId": playground.creator_id,
            "policy": playground.policy,
            "createdAt": playground.created_at,
            "archivedAt": playground.archived_at,
        }

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
        self._audit_code = code
        payload = {"code": code}
        if details is not None:
            payload["details"] = details
        self._respond_json(
            status,
            payload,
            head_only=head_only,
            extra_headers=extra_headers,
        )

    def _deny_hidden_read(self, action, target, head_only=False):
        """Answer one hidden missing/denied read with a single opaque code."""

        self._audit_code = "not_found_or_denied"
        self._audit_override = (action, target)
        self._respond_json(404, {"code": "not_found"}, head_only=head_only)

    def _deny_content_read(
        self, playground_id, commit_id, code=None, head_only=False
    ):
        """Answer an unreadable or missing content read and audit opaquely.

        The client sees the same indistinguishable ``404 not_found`` whether
        the read was denied or the target does not exist.  One audit code
        covers both cases so the event never claims which occurred.  The
        audit event records only the opaque playground/commit target; the
        request path, filename, contents, body, and credential are never
        recorded.
        """

        self._audit_code = "not_found_or_denied"
        self._audit_override = (
            "content.read",
            f"{playground_id}/{commit_id}",
        )
        self._respond_json(404, {"code": "not_found"}, head_only=head_only)

    def _respond_json(self, status, payload, head_only=False, extra_headers=()):
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._respond(
            status,
            text,
            head_only=head_only,
            extra_headers=extra_headers,
            content_type="application/json; charset=utf-8",
        )

    def _send_request_id(self):
        request_id = getattr(self, "_request_id", None)
        if request_id is not None:
            self.send_header("X-Request-Id", request_id)

    def _respond_file(self, data, sha256, head_only=False):
        """Answer a content download with exact bytes and a SHA-256 ETag."""

        self.send_response(200)
        self._send_request_id()
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", f'"{sha256}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self._headers_sent = True
        if not head_only:
            self.wfile.write(data)
        self._audit_status = 200

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
        self._send_request_id()
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
        # Only a completed response is audited as a success.  A failed write
        # leaves the status unset so the error path emits the single event.
        self._audit_status = status

    # Route-aware method boundary for unsupported verbs.
    def do_PUT(self):
        self._dispatch(self._route_put)

    def do_DELETE(self):
        self._dispatch(self._route_delete)

    def do_OPTIONS(self):
        self._dispatch(self._method_not_allowed)

    def log_message(self, format, *args):
        """Suppress the default access log line.

        ``BaseHTTPRequestHandler`` writes the raw request target (including
        any query string) to stderr for every request.  That duplicates data
        the bounded audit path already reports, so it is replaced entirely.
        """
        return


class AuditHTTPServer(ThreadingHTTPServer):
    """Serve requests without leaking tracebacks to the audit sink.

    ``socketserver.BaseServer.handle_error`` prints a full traceback to
    ``sys.stderr``, which is also the default audit output.  That would put
    file paths and source lines into the audit stream, which the audit
    contract forbids.  Unhandled route errors are reported as one bounded,
    traceback-free audit event instead, and a failure while reporting them
    must not change the behavior of the server.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Per-thread handle on the in-flight request so ``handle_error`` can
        # reuse its identity instead of generating a fresh request id.
        self._audit_context = threading.local()

    def handle_error(self, request, client_address):
        log = getattr(self, "audit", None)
        if log is None:
            return

        context = getattr(self, "_audit_context", None)
        handler = getattr(context, "handler", None) if context else None
        if handler is not None and getattr(handler, "_audit_recorded", False):
            # The request already produced its single event; never add a
            # second line for an abort after a completed response.
            return

        request_id = getattr(handler, "_request_id", None)
        actor_id = getattr(handler, "_audit_actor_id", None)
        if request_id is None:
            request_id = log.request_id()
        try:
            log.record(
                request_id=request_id,
                actor_id=actor_id,
                action="request.error",
                target=None,
                status=500,
                code="internal_error",
                started_at=None,
            )
        except Exception:
            pass


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


def make_server(
    host=HOST,
    port=PORT,
    database_path=DATABASE_PATH,
    repository_path=DEFAULT_REPOSITORY_PATH,
    artifacts_path=None,
    generated_path=None,
    web_path=None,
    audit_output=None,
    audit_clock=None,
    audit_request_ids=None,
):
    """Return an initialized HTTP server bound to ``host:port``.

    ``artifacts_path``, ``generated_path`` and ``web_path`` are injected
    runtime directories.  When omitted they default to sibling directories of
    the database, which keeps every deployment path under one data directory.
    The directories are created lazily by the artifact index and
    generated-cache code instead of at startup, so a read-only or unused
    cache never becomes a start-up failure.  ``web_path`` is the installed
    Web bundle (see ``WEB_BUNDLE``); an absent directory is not an error.
    """
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

    if web_path is None:
        web_path = database_path.parent / WEB_DIRECTORY_NAME
    else:
        web_path = Path(web_path)

    repository_path = Path(repository_path)
    repository = open_repository(repository_path)

    if artifacts_path.exists():
        permissions.ensure_private_directory(artifacts_path)

    server = AuditHTTPServer((host, port), Handler)
    server.database_path = database_path
    server.repository_path = repository_path
    server.repository = repository
    server.artifacts_path = artifacts_path
    server.generated_path = generated_path
    server.web_path = web_path
    server.runtime_path = web_path / WEB_RUNTIME_DIRECTORY_NAME
    server.audit = audit.AuditLog(
        output=audit_output,
        clock=audit_clock,
        request_ids=audit_request_ids,
    )
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
