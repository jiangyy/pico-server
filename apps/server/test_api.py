"""HTTP tests for the account API."""

import json
import tempfile
import threading
import unittest
import uuid
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


class ClassApiTest(unittest.TestCase):
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

    def request(self, method, path, payload=None, token=None):
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        status = response.status
        response_headers = {
            key.lower(): value for key, value in response.getheaders()
        }
        connection.close()

        decoded = json.loads(response_body) if response_body else None
        return status, response_headers, decoded

    def unique(self, prefix):
        return f"{prefix}-{uuid.uuid4().hex[:8]}"

    def seed_user(self, role):
        username = self.unique(role)
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
                    role,
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

    def create_class(self, token, teacher_id, name="Class"):
        status, _, body = self.request(
            "POST",
            "/api/v1/admin/classes",
            {"name": name, "teacherId": teacher_id},
            token=token,
        )
        self.assertEqual(status, 201)
        return body["class"]

    def archive_class(self, class_id):
        connection = open_database(self.database_path)
        try:
            with connection:
                connection.execute(
                    "UPDATE classes SET archived_at = ? WHERE id = ?",
                    ("2026-02-01T00:00:00Z", class_id),
                )
        finally:
            connection.close()

    def test_class_lifecycle_and_visibility(self):
        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        other_teacher = self.seed_user("teacher")
        student = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        student_token = self.login_token(student.username)
        teacher_token = self.login_token(teacher.username)

        class_one = self.create_class(admin_token, teacher.id, "Algebra")
        self.assertEqual(class_one["name"], "Algebra")
        self.assertEqual(class_one["teacherId"], teacher.id)
        self.assertIsNone(class_one["archivedAt"])
        self.assertEqual(
            set(class_one),
            {"id", "name", "teacherId", "createdAt", "archivedAt"},
        )

        status, _, body = self.request("GET", "/api/v1/classes", token=admin_token)
        self.assertEqual(status, 200)
        self.assertIn(class_one["id"], [item["id"] for item in body["classes"]])

        status, _, body = self.request(
            "GET", f"/api/v1/classes/{class_one['id']}", token=admin_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["class"]["id"], class_one["id"])

        status, _, body = self.request(
            "PUT",
            f"/api/v1/admin/classes/{class_one['id']}/members/{student.id}",
            token=admin_token,
        )
        self.assertEqual(status, 200)
        membership = body["membership"]
        self.assertEqual(set(membership), {"studentId", "classId", "joinedAt"})
        self.assertEqual(membership["studentId"], student.id)
        self.assertEqual(membership["classId"], class_one["id"])
        self.assertTrue(membership["joinedAt"])

        status, _, body = self.request(
            "GET", "/api/v1/classes", token=student_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in body["classes"]], [class_one["id"]]
        )

        status, _, body = self.request(
            "GET", f"/api/v1/classes/{class_one['id']}", token=student_token
        )
        self.assertEqual(status, 200)

        status, _, body = self.request(
            "GET", "/api/v1/classes", token=teacher_token
        )
        self.assertEqual(
            [item["id"] for item in body["classes"]], [class_one["id"]]
        )

        class_two = self.create_class(admin_token, other_teacher.id, "Geometry")
        status, _, body = self.request(
            "PUT",
            f"/api/v1/admin/classes/{class_two['id']}/members/{student.id}",
            token=admin_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["membership"]["classId"], class_two["id"])

        status, _, body = self.request(
            "GET", f"/api/v1/classes/{class_one['id']}", token=student_token
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))
        status, _, body = self.request(
            "GET", f"/api/v1/classes/{class_two['id']}", token=student_token
        )
        self.assertEqual(status, 200)

        # Removing through the wrong Class is a not-found and changes nothing.
        status, _, body = self.request(
            "DELETE",
            f"/api/v1/admin/classes/{class_one['id']}/members/{student.id}",
            token=admin_token,
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))
        status, _, body = self.request(
            "GET", f"/api/v1/classes/{class_two['id']}", token=student_token
        )
        self.assertEqual(status, 200)

        status, _, body = self.request(
            "DELETE",
            f"/api/v1/admin/classes/{class_two['id']}/members/{student.id}",
            token=admin_token,
        )
        self.assertEqual(status, 204)
        self.assertIsNone(body)

        status, _, body = self.request(
            "GET", f"/api/v1/classes/{class_two['id']}", token=student_token
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))

        # Retrying the removal is an idempotent no-op.
        status, _, body = self.request(
            "DELETE",
            f"/api/v1/admin/classes/{class_two['id']}/members/{student.id}",
            token=admin_token,
        )
        self.assertEqual(status, 204)
        self.assertIsNone(body)

    def test_class_read_and_mutation_authorization(self):
        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        other_teacher = self.seed_user("teacher")
        student = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        teacher_token = self.login_token(teacher.username)
        other_token = self.login_token(other_teacher.username)
        student_token = self.login_token(student.username)

        class_one = self.create_class(admin_token, teacher.id, "Class One")
        class_two = self.create_class(admin_token, other_teacher.id, "Class Two")
        status, _, _ = self.request(
            "PUT",
            f"/api/v1/admin/classes/{class_one['id']}/members/{student.id}",
            token=admin_token,
        )
        self.assertEqual(status, 200)

        status, _, _ = self.request(
            "GET", f"/api/v1/classes/{class_one['id']}", token=teacher_token
        )
        self.assertEqual(status, 200)
        status, _, body = self.request(
            "GET", f"/api/v1/classes/{class_two['id']}", token=teacher_token
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))
        status, _, body = self.request(
            "GET", "/api/v1/classes", token=teacher_token
        )
        self.assertEqual(
            [item["id"] for item in body["classes"]], [class_one["id"]]
        )

        for method, payload in (
            ("PUT", None),
            ("DELETE", None),
            ("PATCH", {"teacherId": other_teacher.id}),
        ):
            path = f"/api/v1/admin/classes/{class_one['id']}/members/{student.id}"
            if method == "PATCH":
                path = f"/api/v1/admin/classes/{class_one['id']}/owner"
            status, _, body = self.request(
                method, path, payload, token=other_token
            )
            self.assertEqual((status, body["code"]), (403, "forbidden"))
            status, _, body = self.request(
                method, path, payload, token=student_token
            )
            self.assertEqual((status, body["code"]), (403, "forbidden"))

        status, _, body = self.request(
            "GET", f"/api/v1/classes/{class_two['id']}", token=student_token
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))

    def test_class_routes_reject_invalid_sessions(self):
        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        student = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        class_one = self.create_class(admin_token, teacher.id, "Class One")

        calls = (
            (
                "POST",
                "/api/v1/admin/classes",
                {"name": "Class", "teacherId": teacher.id},
            ),
            (
                "PATCH",
                f"/api/v1/admin/classes/{class_one['id']}/owner",
                {"teacherId": teacher.id},
            ),
            (
                "PUT",
                f"/api/v1/admin/classes/{class_one['id']}/members/{student.id}",
                None,
            ),
            (
                "DELETE",
                f"/api/v1/admin/classes/{class_one['id']}/members/{student.id}",
                None,
            ),
            ("GET", "/api/v1/classes", None),
            ("GET", f"/api/v1/classes/{class_one['id']}", None),
        )
        for method, path, payload in calls:
            for token in (None, "bogus"):
                status, _, body = self.request(
                    method, path, payload, token=token
                )
                self.assertEqual(
                    (status, body["code"]),
                    (401, "invalid_session"),
                    (method, path, token),
                )

    def test_class_mutation_validation(self):
        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        student = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        class_one = self.create_class(admin_token, teacher.id, "Class One")

        invalid_creates = (
            ({"teacherId": teacher.id}, (400, "invalid_request")),
            ({"name": "Class"}, (400, "invalid_request")),
            ({"name": "", "teacherId": teacher.id}, (400, "invalid_request")),
            (
                {"name": "Class", "teacherId": student.id},
                (400, "invalid_request"),
            ),
            (
                {"name": "Class", "teacherId": "missing-teacher"},
                (404, "not_found"),
            ),
        )
        for payload, expected in invalid_creates:
            status, _, body = self.request(
                "POST", "/api/v1/admin/classes", payload, token=admin_token
            )
            self.assertEqual((status, body["code"]), expected, payload)

        status, _, _ = self.request(
            "PATCH",
            f"/api/v1/admin/classes/{class_one['id']}/owner",
            {"teacherId": teacher.id},
            token=admin_token,
        )
        self.assertEqual(status, 200)
        status, _, body = self.request(
            "PATCH",
            "/api/v1/admin/classes/missing/owner",
            {"teacherId": teacher.id},
            token=admin_token,
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))
        for payload, expected in (
            ({}, (400, "invalid_request")),
            ({"teacherId": student.id}, (400, "invalid_request")),
            ({"teacherId": "missing-teacher"}, (404, "not_found")),
        ):
            status, _, body = self.request(
                "PATCH",
                f"/api/v1/admin/classes/{class_one['id']}/owner",
                payload,
                token=admin_token,
            )
            self.assertEqual((status, body["code"]), expected, payload)

        mutations = (
            (
                "PUT",
                "/api/v1/admin/classes/missing/members/" + student.id,
                None,
                (404, "not_found"),
            ),
            (
                "PUT",
                f"/api/v1/admin/classes/{class_one['id']}/members/missing",
                None,
                (404, "not_found"),
            ),
            (
                "PUT",
                f"/api/v1/admin/classes/{class_one['id']}/members/{teacher.id}",
                None,
                (400, "invalid_request"),
            ),
            (
                "DELETE",
                f"/api/v1/admin/classes/{class_one['id']}/members/missing",
                None,
                (404, "not_found"),
            ),
            (
                "DELETE",
                "/api/v1/admin/classes/missing/members/" + student.id,
                None,
                (404, "not_found"),
            ),
            (
                "DELETE",
                f"/api/v1/admin/classes/{class_one['id']}/members/{teacher.id}",
                None,
                (400, "invalid_request"),
            ),
        )
        for method, path, payload, expected in mutations:
            status, _, body = self.request(
                method, path, payload, token=admin_token
            )
            self.assertEqual((status, body["code"]), expected, (method, path))

    def test_class_mutations_reject_archived_class(self):
        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        student = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        student_token = self.login_token(student.username)

        archived = self.create_class(admin_token, teacher.id, "Archived")
        self.archive_class(archived["id"])
        status, _, body = self.request(
            "PUT",
            f"/api/v1/admin/classes/{archived['id']}/members/{student.id}",
            token=admin_token,
        )
        self.assertEqual((status, body["code"]), (409, "class_archived"))

        live = self.create_class(admin_token, teacher.id, "Live")
        status, _, _ = self.request(
            "PUT",
            f"/api/v1/admin/classes/{live['id']}/members/{student.id}",
            token=admin_token,
        )
        self.assertEqual(status, 200)
        self.archive_class(live["id"])

        status, _, body = self.request(
            "DELETE",
            f"/api/v1/admin/classes/{live['id']}/members/{student.id}",
            token=admin_token,
        )
        self.assertEqual((status, body["code"]), (409, "class_archived"))

        status, _, _ = self.request(
            "GET", f"/api/v1/classes/{live['id']}", token=student_token
        )
        self.assertEqual(status, 200)

    def test_class_route_methods_and_paths(self):
        method_not_allowed = (
            ("GET", "/api/v1/admin/classes", "POST"),
            ("PUT", "/api/v1/admin/classes", "POST"),
            ("DELETE", "/api/v1/admin/classes", "POST"),
            ("PATCH", "/api/v1/admin/classes", "POST"),
            ("POST", "/api/v1/admin/classes/some-id/owner", "PATCH"),
            ("GET", "/api/v1/admin/classes/some-id/owner", "PATCH"),
            ("DELETE", "/api/v1/admin/classes/some-id/owner", "PATCH"),
            ("PUT", "/api/v1/admin/classes/some-id/owner", "PATCH"),
            ("POST", "/api/v1/admin/classes/c/members/s", "PUT, DELETE"),
            ("GET", "/api/v1/admin/classes/c/members/s", "PUT, DELETE"),
            ("PATCH", "/api/v1/admin/classes/c/members/s", "PUT, DELETE"),
            ("POST", "/api/v1/classes", "GET, HEAD"),
            ("PUT", "/api/v1/classes", "GET, HEAD"),
            ("PATCH", "/api/v1/classes", "GET, HEAD"),
            ("DELETE", "/api/v1/classes/some-id", "GET, HEAD"),
            ("POST", "/api/v1/classes/some-id", "GET, HEAD"),
        )
        for method, path, allow in method_not_allowed:
            status, headers, body = self.request(method, path)
            self.assertEqual(status, 405, (method, path))
            self.assertEqual(headers.get("allow"), allow, (method, path))
            self.assertEqual(body["code"], "method_not_allowed")

        not_found = (
            ("GET", "/api/v1/admin/classes/some-id"),
            ("PATCH", "/api/v1/admin/classes/some-id"),
            ("POST", "/api/v1/admin/classes/some-id/members/a/b"),
            ("PUT", "/api/v1/admin/classes/some-id/members"),
            ("PUT", "/api/v1/admin/classes/some-id/members/"),
            ("PUT", "/api/v1/admin/classes//members/some-student"),
            ("DELETE", "/api/v1/admin/classes/some-id/members/a/b"),
            ("GET", "/api/v1/classes/"),
            ("GET", "/api/v1/classes/a/b"),
            ("GET", "/api/v1/admin/classes/some-id/owner/extra"),
        )
        for method, path in not_found:
            status, _, body = self.request(method, path)
            self.assertEqual((status, body["code"]), (404, "not_found"), (method, path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
