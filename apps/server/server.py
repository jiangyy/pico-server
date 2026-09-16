"""PicoPG HTTP service using only the Python standard library."""

import hashlib
import json
import threading
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import audit
import authorization
import manifests
import submissions
import works
from accounts import (
    ForbiddenActionError,
    InvalidRoleError,
    MissingUserError,
    RoleConflictError,
    UsernameTakenError,
    change_user_role,
    create_user,
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
    create_playground,
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
    SubmissionNotAllowedError,
    UnknownReleaseError,
    UnknownWorkCommitError,
    create_submission,
    get_submission,
    list_submissions,
    submission_to_mapping,
)


HOST = "127.0.0.1"
PORT = 8001
DATABASE_PATH = Path("data/app.db")
ARTIFACTS_DIRECTORY_NAME = "artifacts"
GENERATED_DIRECTORY_NAME = "generated"
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
CLASS_OWNER_SUFFIX = "/owner"
CLASS_MEMBERS_SEGMENT = "/members/"
CLASSES_PATH = "/api/v1/classes"
PLAYGROUNDS_PATH = "/api/v1/playgrounds"
PLAYGROUND_COMMITS_SUFFIX = "/commits"
WORKS_PATH = "/api/v1/works"
WORK_SYNC_SUFFIX = "/sync"
WORK_RESTORE_SUFFIX = "/restore"
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


def _allowed_methods(path):
    """Return the methods a known path accepts, or ``None`` when unknown."""
    methods = ROUTE_METHODS.get(path)
    if methods is not None:
        return methods
    if path == ADMIN_USERS_PATH:
        return ("POST",)
    if _match_admin_user_role(path) is not None:
        return ("PATCH",)
    if path == ADMIN_CLASSES_PATH:
        return ("POST",)
    if _match_class_owner(path) is not None:
        return ("PATCH",)
    if _match_class_member(path) is not None:
        return ("PUT", "DELETE")
    if path == CLASSES_PATH:
        return ALLOWED_METHODS
    if _match_class(path) is not None:
        return ALLOWED_METHODS
    if path == PLAYGROUNDS_PATH:
        return ("POST",)
    if _match_playground_commit(path) is not None:
        return ("POST",)
    if _match_work_sync(path) is not None:
        return ("POST",)
    if _match_work_restore(path) is not None:
        return ("POST",)
    if _match_manifest(path) is not None:
        return ALLOWED_METHODS
    if _match_commit_files(path) is not None:
        return ALLOWED_METHODS
    if _match_playground(path) is not None:
        return ("PATCH",)
    if _match_class_releases(path) is not None:
        return ("POST",)
    if path == CATALOG_PATH:
        return ALLOWED_METHODS
    if path == RELEASES_PATH:
        return ALLOWED_METHODS
    if _match_release_submissions(path) is not None:
        return ("GET", "HEAD", "POST")
    if _match_submission_comments(path) is not None:
        return ("GET", "HEAD", "POST")
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
        if log is None:
            route(**kwargs)
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
        finally:
            self._emit_audit(log, started_at)

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
                # worth auditing; every other authorization failure is a
                # bounded event.
                if status not in (401, 403) or path == "/api/v1/login":
                    return
                action = "request.denied"
        self._audit_recorded = True
        log.record(
            request_id=self._request_id,
            actor_id=self._audit_actor_id,
            action=action,
            target=target,
            status=status,
            code=self._audit_code,
            started_at=started_at,
        )

    def _route_get(self, head_only=False):
        path = urlsplit(self.path).path

        if path == "/":
            self._respond(200, "Pico service is running\n", head_only=head_only)
        elif path == "/healthz":
            self._respond(200, "ok\n", head_only=head_only)
        elif path == "/api/v1/me":
            self._get_me(head_only=head_only)
        elif path == CLASSES_PATH:
            self._get_classes(head_only=head_only)
        elif _match_class(path) is not None:
            self._get_class(_match_class(path), head_only=head_only)
        elif path == CATALOG_PATH:
            self._get_catalog(head_only=head_only)
        elif _match_manifest(path) is not None:
            self._get_manifest(*_match_manifest(path), head_only=head_only)
        elif _match_commit_files(path) is not None:
            self._get_commit_file(
                *_match_commit_files(path), head_only=head_only
            )
        elif path == RELEASES_PATH:
            self._get_releases(head_only=head_only)
        elif _match_release_submissions(path) is not None:
            self._get_release_submissions(
                _match_release_submissions(path), head_only=head_only
            )
        elif _match_submission_comments(path) is not None:
            self._get_submission_comments(
                _match_submission_comments(path), head_only=head_only
            )
        elif _match_submission(path) is not None:
            self._get_submission(
                _match_submission(path), head_only=head_only
            )
        elif _match_release(path) is not None:
            self._get_release(_match_release(path), head_only=head_only)
        elif _allowed_methods(path) is not None:
            self._method_not_allowed()
        else:
            self._not_found()

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

        try:
            with self._database() as connection:
                classes = list_classes(connection, actor)
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        self._respond_json(
            200,
            {"classes": [self._class_payload(item) for item in classes]},
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

    def _get_catalog(self, head_only=False):
        actor = self._optional_actor(head_only=head_only)
        if actor is _AUTH_FAILED:
            return

        try:
            with self._database() as connection:
                entries = self._build_catalog(connection, actor)
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        self._respond_json(
            200, {"catalog": entries}, head_only=head_only
        )

    def _build_catalog(self, connection, actor):
        """Return the deduplicated, stably ordered catalog for ``actor``.

        Public unarchived Playgrounds contribute their current commit, and an
        authenticated actor additionally contributes the commits of every
        Release it is currently authorized to read.  An item is identified by
        the exact ``(playgroundId, commitId)`` pair, so a commit reachable both
        publicly and through a Release appears once with all its Release ids.
        """

        entries = {}
        rows = connection.execute(
            "SELECT id FROM playgrounds"
            " WHERE policy = ? AND archived_at IS NULL"
            " ORDER BY id",
            (PUBLIC_POLICY,),
        ).fetchall()
        for row in rows:
            playground_id = row["id"]
            if not valid_playground_id(playground_id):
                raise RepositoryError(
                    "stored playground identifier is invalid"
                )
            commit_id = manifests.current_playground_commit(
                self.server.repository, row["id"]
            )
            if commit_id is None:
                continue
            entries[(row["id"], commit_id)] = []

        if actor is not None:
            for release in list_releases(connection, actor):
                key = (release.playground_id, release.playground_commit_id)
                entries.setdefault(key, []).append(release.id)

        catalog = []
        for playground_id, commit_id in sorted(entries):
            catalog.append(
                {
                    "playgroundId": playground_id,
                    "commitId": commit_id,
                    "releaseIds": sorted(
                        set(entries[(playground_id, commit_id)])
                    ),
                }
            )
        return catalog

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
            self._api_error(404, "not_found", head_only=head_only)
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
            self._api_error(404, "not_found", head_only=head_only)
            return

        self._respond_file(content.data, content.sha256, head_only=head_only)

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
            self._api_error(400, "invalid_request")
            return

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
            self._api_error(400, "invalid_request")
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
            self._api_error(400, "invalid_request")
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

    def _get_releases(self, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return

        try:
            with self._database() as connection:
                releases = list_releases(connection, actor)
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        self._respond_json(
            200,
            {"releases": [release_to_mapping(item) for item in releases]},
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
            self._api_error(404, "not_found", head_only=head_only)
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        self._respond_json(
            200,
            {"release": release_to_mapping(release)},
            head_only=head_only,
        )

    def _get_release_submissions(self, release_id, head_only=False):
        actor = self._authenticate(head_only=head_only)
        if actor is None:
            return

        try:
            with self._database() as connection:
                release = get_release(connection, actor, release_id)
                records = list_submissions(connection, actor)
        except MissingReleaseError:
            self._api_error(404, "not_found", head_only=head_only)
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        listed = [
            record for record in records if record.release_id == release.id
        ]
        self._respond_json(
            200,
            {"submissions": [submission_to_mapping(item) for item in listed]},
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
            self._api_error(404, "not_found", head_only=head_only)
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

        try:
            with self._database() as connection:
                records = list_comments(connection, actor, submission_id)
        except MissingSubmissionError:
            self._api_error(404, "not_found", head_only=head_only)
            return
        except InvalidCommentRequestError:
            self._api_error(400, "invalid_request", head_only=head_only)
            return
        except ForbiddenActionError:
            self._api_error(403, "forbidden", head_only=head_only)
            return

        self._respond_json(
            200,
            {"comments": [comment_to_mapping(item) for item in records]},
            head_only=head_only,
        )

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
            self._api_error(400, "invalid_request")
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
            self._api_error(400, "invalid_request")
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
        self._respond(204, "")

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

    def _not_found(self):
        if urlsplit(self.path).path.startswith("/api/"):
            self._api_error(404, "not_found")
        else:
            self._respond(404, "not found\n")

    def _method_not_allowed(self):
        path = urlsplit(self.path).path
        methods = _allowed_methods(path)
        if methods is None:
            self._not_found()
            return

        headers = (("Allow", ", ".join(methods)),)
        if path.startswith("/api/"):
            self._api_error(405, "method_not_allowed", extra_headers=headers)
        else:
            self._respond(
                405,
                "method not allowed\n",
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

    def _deny_content_read(
        self, playground_id, commit_id, code, head_only=False
    ):
        """Answer an unreadable or missing content read and audit opaquely.

        The client sees the same indistinguishable ``404 not_found`` whether
        the read was denied or the target does not exist.  ``code``
        distinguishes the two for the audit stream only.  The audit event
        records only the opaque playground/commit target and the result code;
        the request path, filename, contents, body, and credential are never
        recorded.
        """

        self._audit_code = code
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


def make_server(
    host=HOST,
    port=PORT,
    database_path=DATABASE_PATH,
    repository_path=DEFAULT_REPOSITORY_PATH,
    artifacts_path=None,
    generated_path=None,
    audit_output=None,
    audit_clock=None,
    audit_request_ids=None,
):
    """Return an initialized HTTP server bound to ``host:port``.

    ``artifacts_path`` and ``generated_path`` are injected runtime directories.
    When omitted they default to sibling directories of the database, which
    keeps every deployment path under one data directory.  The directories are
    created lazily by the artifact index and generated-cache code instead of at
    startup, so a read-only or unused cache never becomes a start-up failure.
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

    repository_path = Path(repository_path)
    repository = open_repository(repository_path)

    server = AuditHTTPServer((host, port), Handler)
    server.database_path = database_path
    server.repository_path = repository_path
    server.repository = repository
    server.artifacts_path = artifacts_path
    server.generated_path = generated_path
    server.audit = audit.AuditLog(
        output=audit_output,
        clock=audit_clock,
        request_ids=audit_request_ids,
    )
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
