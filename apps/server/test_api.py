"""HTTP tests for the account API."""

import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

import accounts
from database import open_database
from server import MAX_JSON_BODY_BYTES, make_server


class AccountApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.database_path = Path(cls.temporary_directory.name) / "app.db"
        repository_path = Path(cls.temporary_directory.name) / "picopg.git"
        cls.server = make_server(
            host="127.0.0.1",
            port=0,
            database_path=cls.database_path,
            repository_path=repository_path,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temporary_directory.cleanup()

    def request(
        self,
        method,
        path,
        payload=None,
        token=None,
        raw_body=None,
        content_type="application/json",
    ):
        headers = {}
        body = raw_body
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            headers["Content-Type"] = content_type
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        status = response.status
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()

        decoded = json.loads(response_body) if response_body else None
        return status, response_headers, decoded

    def register(self, username):
        return self.request(
            "POST",
            "/api/v1/register",
            {"username": username, "password": "secret"},
        )

    def seed_admin(self, username):
        """Insert an Admin account directly, since no bootstrap API exists."""
        connection = open_database(self.database_path)
        try:
            with connection:
                return accounts.create_user(
                    connection,
                    accounts.User(
                        id="seed-actor",
                        username="seed-actor",
                        role="admin",
                        created_at="2026-01-01T00:00:00Z",
                    ),
                    username,
                    "secret",
                    "admin",
                )
        finally:
            connection.close()

    def login_token(self, username):
        status, _, body = self.request(
            "POST",
            "/api/v1/login",
            {"username": username, "password": "secret"},
        )
        self.assertEqual(status, 200)
        return body["sessionToken"]

    def test_register_creates_only_students(self):
        status, _, body = self.request(
            "POST",
            "/api/v1/register",
            {"username": "alice", "password": "secret", "role": "admin"},
        )

        self.assertEqual(status, 201)
        self.assertEqual(body["user"]["username"], "alice")
        self.assertEqual(body["user"]["role"], "student")

        duplicate_status, _, duplicate = self.register("alice")
        self.assertEqual(duplicate_status, 409)
        self.assertEqual(duplicate["code"], "username_taken")

    def test_login_me_and_logout(self):
        self.register("bob")
        status, _, body = self.request(
            "POST",
            "/api/v1/login",
            {"username": "bob", "password": "secret"},
        )

        self.assertEqual(status, 200)
        token = body["sessionToken"]
        self.assertTrue(token)
        self.assertEqual(body["user"]["username"], "bob")

        me_status, _, me = self.request("GET", "/api/v1/me", token=token)
        self.assertEqual(me_status, 200)
        self.assertEqual(me["user"], body["user"])

        logout_status, _, logout = self.request(
            "POST",
            "/api/v1/logout",
            token=token,
        )
        self.assertEqual(logout_status, 204)
        self.assertIsNone(logout)

        revoked_status, _, revoked = self.request("GET", "/api/v1/me", token=token)
        self.assertEqual(revoked_status, 401)
        self.assertEqual(revoked["code"], "invalid_session")

        retry_status, _, _ = self.request("POST", "/api/v1/logout", token=token)
        self.assertEqual(retry_status, 204)

    def test_login_uses_one_error_for_invalid_credentials(self):
        self.register("carol")
        attempts = (
            {"username": "carol", "password": "wrong"},
            {"username": "missing", "password": "secret"},
        )

        responses = [
            self.request("POST", "/api/v1/login", attempt) for attempt in attempts
        ]
        self.assertEqual(
            [(status, body["code"]) for status, _, body in responses],
            [(401, "invalid_credentials"), (401, "invalid_credentials")],
        )

    def test_invalid_json_and_missing_auth_are_rejected(self):
        status, _, body = self.request(
            "POST",
            "/api/v1/register",
            raw_body=b"{",
        )
        self.assertEqual((status, body["code"]), (400, "invalid_json"))

        status, _, body = self.request("GET", "/api/v1/me")
        self.assertEqual((status, body["code"]), (401, "invalid_session"))

    def test_json_requests_enforce_media_type_and_size(self):
        status, _, body = self.request(
            "POST",
            "/api/v1/register",
            raw_body=b"{}",
            content_type="text/plain",
        )
        self.assertEqual(
            (status, body["code"]),
            (415, "unsupported_media_type"),
        )

        status, _, body = self.request(
            "POST",
            "/api/v1/register",
            raw_body=b"x" * (MAX_JSON_BODY_BYTES + 1),
        )
        self.assertEqual((status, body["code"]), (413, "request_too_large"))

    def test_admin_creates_user_and_updates_role(self):
        self.seed_admin("admin-one")
        token = self.login_token("admin-one")

        status, _, body = self.request(
            "POST",
            "/api/v1/admin/users",
            {
                "username": "teacher-one",
                "password": "secret",
                "role": "teacher",
                "ignored": "value",
            },
            token=token,
        )
        self.assertEqual(status, 201)
        teacher = body["user"]
        self.assertEqual(teacher["username"], "teacher-one")
        self.assertEqual(teacher["role"], "teacher")
        self.assertEqual(set(teacher), {"id", "username", "role", "createdAt"})
        self.assertNotIn("password", teacher)
        self.assertNotIn("passwordHash", teacher)

        status, _, body = self.request(
            "PATCH",
            f"/api/v1/admin/users/{teacher['id']}/role",
            {"role": "admin"},
            token=token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["id"], teacher["id"])
        self.assertEqual(body["user"]["role"], "admin")
        self.assertEqual(
            set(body["user"]),
            {"id", "username", "role", "createdAt"},
        )

    def test_admin_authorization_precedes_mutation(self):
        self.register("victim")
        victim_token = self.login_token("victim")
        _, _, me = self.request("GET", "/api/v1/me", token=victim_token)
        victim_id = me["user"]["id"]

        status, _, body = self.request(
            "POST",
            "/api/v1/admin/users",
            {"username": "escalated", "password": "secret", "role": "admin"},
            token=victim_token,
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))

        status, _, body = self.request(
            "PATCH",
            f"/api/v1/admin/users/{victim_id}/role",
            {"role": "admin"},
            token=victim_token,
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))

        _, _, me = self.request("GET", "/api/v1/me", token=victim_token)
        self.assertEqual(me["user"]["role"], "student")

        # The forbidden create never happened: an Admin still takes the name.
        self.seed_admin("admin-two")
        admin_token = self.login_token("admin-two")
        status, _, _ = self.request(
            "POST",
            "/api/v1/admin/users",
            {"username": "escalated", "password": "secret", "role": "admin"},
            token=admin_token,
        )
        self.assertEqual(status, 201)

    def test_admin_endpoints_reject_invalid_sessions(self):
        create = {
            "username": "nobody",
            "password": "secret",
            "role": "student",
        }
        for token in (None, "bogus"):
            status, _, body = self.request(
                "POST",
                "/api/v1/admin/users",
                create,
                token=token,
            )
            self.assertEqual((status, body["code"]), (401, "invalid_session"))

            status, _, body = self.request(
                "PATCH",
                "/api/v1/admin/users/whoever/role",
                {"role": "student"},
                token=token,
            )
            self.assertEqual((status, body["code"]), (401, "invalid_session"))

    def test_admin_create_user_rejects_invalid_bodies(self):
        self.seed_admin("admin-three")
        token = self.login_token("admin-three")

        cases = (
            {"password": "secret", "role": "student"},
            {"username": "new-user", "role": "student"},
            {"username": "new-user", "password": "secret"},
            {"username": "new-user", "password": "secret", "role": "wizard"},
            {"username": "new-user", "password": "secret", "role": 7},
            {"username": 7, "password": "secret", "role": "student"},
        )
        for payload in cases:
            status, _, body = self.request(
                "POST",
                "/api/v1/admin/users",
                payload,
                token=token,
            )
            self.assertEqual(
                (status, body["code"]),
                (400, "invalid_request"),
                payload,
            )

        valid = {
            "username": "new-user",
            "password": "secret",
            "role": "student",
        }
        status, _, body = self.request(
            "POST", "/api/v1/admin/users", valid, token=token
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["user"]["role"], "student")

        status, _, body = self.request(
            "POST", "/api/v1/admin/users", valid, token=token
        )
        self.assertEqual((status, body["code"]), (409, "username_taken"))

    def test_admin_role_update_failures(self):
        self.seed_admin("admin-four")
        token = self.login_token("admin-four")

        cases = (
            ({"role": "teacher"}, (404, "not_found")),
            ({}, (400, "invalid_request")),
            ({"role": "wizard"}, (400, "invalid_request")),
            ({"role": 7}, (400, "invalid_request")),
        )
        for payload, expected in cases:
            status, _, body = self.request(
                "PATCH",
                "/api/v1/admin/users/does-not-exist/role",
                payload,
                token=token,
            )
            self.assertEqual((status, body["code"]), expected, payload)

    def test_admin_role_update_reports_class_conflict(self):
        self.seed_admin("admin-five")
        token = self.login_token("admin-five")

        status, _, body = self.request(
            "POST",
            "/api/v1/admin/users",
            {
                "username": "teacher-five",
                "password": "secret",
                "role": "teacher",
            },
            token=token,
        )
        self.assertEqual(status, 201)
        teacher = body["user"]

        connection = open_database(self.database_path)
        try:
            with connection:
                connection.execute(
                    "INSERT INTO classes (id, name, teacher_id, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        "class-five",
                        "Class Five",
                        teacher["id"],
                        "2026-01-01T00:00:00Z",
                    ),
                )
        finally:
            connection.close()

        status, _, body = self.request(
            "PATCH",
            f"/api/v1/admin/users/{teacher['id']}/role",
            {"role": "student"},
            token=token,
        )
        self.assertEqual((status, body["code"]), (409, "role_conflict"))

        teacher_token = self.login_token("teacher-five")
        _, _, me = self.request("GET", "/api/v1/me", token=teacher_token)
        self.assertEqual(me["user"]["role"], "teacher")

    def test_admin_route_methods_and_paths(self):
        for method, path, allow in (
            ("GET", "/api/v1/admin/users", "POST"),
            ("PUT", "/api/v1/admin/users", "POST"),
            ("DELETE", "/api/v1/admin/users", "POST"),
            ("PATCH", "/api/v1/admin/users", "POST"),
            ("POST", "/api/v1/admin/users/some-id/role", "PATCH"),
            ("GET", "/api/v1/admin/users/some-id/role", "PATCH"),
            ("DELETE", "/api/v1/admin/users/some-id/role", "PATCH"),
            ("PATCH", "/api/v1/register", "POST"),
        ):
            status, headers, body = self.request(method, path)
            self.assertEqual(status, 405, (method, path))
            self.assertEqual(headers.get("allow"), allow, (method, path))
            self.assertEqual(body["code"], "method_not_allowed")

        for path in (
            "/api/v1/admin/users/some-id",
            "/api/v1/admin/users/some-id/role/extra",
            "/api/v1/admin/users/some-id/roles",
            "/api/v1/admin/users/-/role/",
            "/api/v1/admin/users//role",
            "/api/v1/admin/unknown",
        ):
            status, _, body = self.request("PATCH", path, {"role": "student"})
            self.assertEqual((status, body["code"]), (404, "not_found"), path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
