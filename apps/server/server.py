"""PicoPG HTTP service using only the Python standard library."""

import json
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

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
from database import open_database
from repository import DEFAULT_REPOSITORY_PATH, open_repository
from sessions import (
    InvalidCredentialsError,
    InvalidSessionError,
    authenticate_session,
    login,
    revoke_session,
)


HOST = "127.0.0.1"
PORT = 8001
DATABASE_PATH = Path("data/app.db")
MAX_JSON_BODY_BYTES = 16 * 1024

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


def _allowed_methods(path):
    """Return the methods a known path accepts, or ``None`` when unknown."""
    methods = ROUTE_METHODS.get(path)
    if methods is not None:
        return methods
    if path == ADMIN_USERS_PATH:
        return ("POST",)
    if _match_admin_user_role(path) is not None:
        return ("PATCH",)
    return None


class Handler(BaseHTTPRequestHandler):
    """Route public pages and the first account API."""

    def do_GET(self):
        self._route_get()

    def do_HEAD(self):
        self._route_get(head_only=True)

    def do_POST(self):
        self._route_post()

    def do_PATCH(self):
        self._route_patch()

    def _route_get(self, head_only=False):
        path = urlsplit(self.path).path

        if path == "/":
            self._respond(200, "Pico service is running\n", head_only=head_only)
        elif path == "/healthz":
            self._respond(200, "ok\n", head_only=head_only)
        elif path == "/api/v1/me":
            self._get_me(head_only=head_only)
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
        elif _allowed_methods(path) is not None:
            self._method_not_allowed()
        else:
            self._not_found()

    def _route_patch(self):
        path = urlsplit(self.path).path
        user_id = _match_admin_user_role(path)
        if user_id is not None:
            self._patch_admin_user_role(user_id)
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

    def _authenticate(self, head_only=False):
        token = self._bearer_token()
        if token is None:
            self._api_error(401, "invalid_session", head_only=head_only)
            return None

        try:
            with self._database() as connection:
                return authenticate_session(connection, token)
        except InvalidSessionError:
            self._api_error(401, "invalid_session", head_only=head_only)
            return None

    def _post_logout(self):
        token = self._bearer_token()
        if token is None:
            self._api_error(401, "invalid_session")
            return

        with self._database() as connection:
            revoke_session(connection, token)
        self._respond(204, "")

    def _read_json(self):
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
        if content_length > MAX_JSON_BODY_BYTES:
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

    def _api_error(self, status, code, head_only=False, extra_headers=()):
        self._respond_json(
            status,
            {"code": code},
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
        if not head_only:
            self.wfile.write(body)

    # Route-aware method boundary for unsupported verbs.
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed


def make_server(
    host=HOST,
    port=PORT,
    database_path=DATABASE_PATH,
    repository_path=DEFAULT_REPOSITORY_PATH,
):
    """Return an initialized HTTP server bound to ``host:port``."""
    database_path = Path(database_path)
    database = open_database(database_path)
    database.close()

    repository_path = Path(repository_path)
    repository = open_repository(repository_path)

    server = ThreadingHTTPServer((host, port), Handler)
    server.database_path = database_path
    server.repository_path = repository_path
    server.repository = repository
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
