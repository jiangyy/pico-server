"""HTTP tests for the account API."""

import base64
import hashlib
import io
import json
import subprocess
import tempfile
import threading
import unittest
import uuid
import zipfile
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

import accounts
import bundles
import generated
import repository
import works
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


class PlaygroundApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.database_path = Path(cls.temporary_directory.name) / "app.db"
        cls.repository_path = Path(cls.temporary_directory.name) / "picopg.git"
        cls.server = make_server(
            host="127.0.0.1",
            port=0,
            database_path=cls.database_path,
            repository_path=cls.repository_path,
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
        content_type="application/zip",
        extra_headers=None,
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
        if extra_headers is not None:
            headers.update(extra_headers)

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

    def create_playground(self, token, playground_id):
        status, _, body = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": playground_id},
            token=token,
        )
        self.assertEqual(status, 201)
        return body["playground"]

    # --- Playground commit upload ----------------------------------------

    MANIFEST = b'{"schemaVersion":1,"runtime":"python"}'

    def build_zip(self, entries=None, compression=zipfile.ZIP_DEFLATED):
        if entries is None:
            entries = (
                ("picopg.json", self.MANIFEST),
                ("course/module/app.py", b"print(1)"),
                ("starter/setup.py", b"setup"),
            )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return buffer.getvalue()

    def build_zip_with_info(self, entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for info, data in entries:
                archive.writestr(info, data)
        return buffer.getvalue()

    def publish(
        self,
        token,
        playground_id,
        body,
        key,
        content_type="application/zip",
    ):
        return self.request(
            "POST",
            f"/api/v1/playgrounds/{playground_id}/commits",
            raw_body=body,
            token=token,
            content_type=content_type,
            extra_headers={"Idempotency-Key": key},
        )

    def count_idempotency_rows(self, endpoint):
        connection = open_database(self.database_path)
        try:
            return connection.execute(
                "SELECT COUNT(*) AS total FROM idempotency_results"
                " WHERE endpoint = ?",
                (endpoint,),
            ).fetchone()["total"]
        finally:
            connection.close()

    def test_publish_commit_success_and_exact_retry(self):
        admin = self.seed_user("admin")
        token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(token, playground_id)
        body = self.build_zip()

        status, _, response = self.publish(token, playground_id, body, "key-1")
        self.assertEqual(status, 201)
        self.assertEqual(
            set(response), {"playgroundId", "commitId", "noOp"}
        )
        self.assertEqual(response["playgroundId"], playground_id)
        self.assertTrue(response["commitId"])
        self.assertFalse(response["noOp"])

        # The returned commit is the real Playground ref head.
        ref = repository.Repository(self.repository_path).playground_ref(
            playground_id
        )
        completed = subprocess.run(
            ["git", "-C", str(self.repository_path), "rev-parse", ref],
            capture_output=True,
            check=True,
        )
        self.assertEqual(
            completed.stdout.strip().decode(), response["commitId"]
        )

        # An exact retry returns the stored status and body, not a new commit.
        retry_status, _, retry = self.publish(
            token, playground_id, body, "key-1"
        )
        self.assertEqual(retry_status, 201)
        self.assertEqual(retry, response)
        self.assertEqual(
            self.count_idempotency_rows(
                f"POST /api/v1/playgrounds/{playground_id}/commits"
            ),
            1,
        )

    def test_repacked_identical_bundle_is_the_same_request(self):
        admin = self.seed_user("admin")
        token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(token, playground_id)

        stored = self.build_zip(compression=zipfile.ZIP_STORED)
        deflated = self.build_zip(compression=zipfile.ZIP_DEFLATED)
        self.assertNotEqual(stored, deflated)

        status, _, first = self.publish(
            token, playground_id, stored, "key-repack"
        )
        self.assertEqual(status, 201)
        retry_status, _, second = self.publish(
            token, playground_id, deflated, "key-repack"
        )
        self.assertEqual(retry_status, 201)
        self.assertEqual(second, first)

    def test_publishing_same_content_with_new_key_is_a_noop(self):
        admin = self.seed_user("admin")
        token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(token, playground_id)

        first = self.publish(
            token, playground_id, self.build_zip(), "key-a"
        )[2]
        second_status, _, second = self.publish(
            token,
            playground_id,
            self.build_zip(compression=zipfile.ZIP_STORED),
            "key-b",
        )
        self.assertEqual(second_status, 201)
        self.assertTrue(second["noOp"])
        self.assertEqual(second["commitId"], first["commitId"])

    def test_same_key_with_different_content_conflicts(self):
        admin = self.seed_user("admin")
        token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(token, playground_id)

        self.publish(token, playground_id, self.build_zip(), "key-conflict")
        changed = self.build_zip(
            (
                ("picopg.json", self.MANIFEST),
                ("course/module/app.py", b"print(2)"),
            )
        )
        status, _, response = self.publish(
            token, playground_id, changed, "key-conflict"
        )
        self.assertEqual(
            (status, response["code"]), (409, "idempotency_conflict")
        )

    def test_invalid_bundles_return_a_bounded_reason(self):
        admin = self.seed_user("admin")
        token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(token, playground_id)

        symlink = zipfile.ZipInfo("course/link")
        symlink.external_attr = (0o120777) << 16

        cases = (
            (b"not a zip", bundles.REASON_ARCHIVE_INVALID),
            (
                self.build_zip((("course/module/app.py", b"x"),)),
                bundles.REASON_MISSING_MANIFEST,
            ),
            (
                self.build_zip(
                    (
                        ("picopg.json", self.MANIFEST),
                        ("../escape.py", b"x"),
                    )
                ),
                bundles.REASON_PATH_DOT_SEGMENT,
            ),
            (
                self.build_zip_with_info(((symlink, b"../../outside"),)),
                bundles.REASON_ARCHIVE_LINK,
            ),
            (
                self.build_zip(
                    (
                        ("picopg.json", self.MANIFEST),
                        ("course/zeros.bin", b"\x00" * 65536),
                    )
                ),
                bundles.REASON_ARCHIVE_SIZE_RATIO,
            ),
        )
        for body, reason in cases:
            with self.subTest(reason=reason):
                status, _, response = self.publish(
                    token, playground_id, body, self.unique("key")
                )
                self.assertEqual(status, 400)
                self.assertEqual(response["code"], "invalid_bundle")
                self.assertEqual(response["details"], {"reason": reason})

    def test_invalid_bundle_error_does_not_echo_content(self):
        admin = self.seed_user("admin")
        token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(token, playground_id)

        secret = "SECRET-MANIFEST-SENTINEL"
        body = self.build_zip(
            (("picopg.json", ("{" + secret + ":").encode("utf-8")),)
        )
        status, _, response = self.publish(
            token, playground_id, body, self.unique("key")
        )
        self.assertEqual(status, 400)
        self.assertNotIn(secret, json.dumps(response))

    def test_publish_request_requirements(self):
        admin = self.seed_user("admin")
        token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(token, playground_id)
        path = f"/api/v1/playgrounds/{playground_id}/commits"
        body = self.build_zip()

        status, _, response = self.publish(
            token,
            playground_id,
            body,
            "key",
            content_type="application/octet-stream",
        )
        self.assertEqual(
            (status, response["code"]), (415, "unsupported_media_type")
        )

        status, _, response = self.request(
            "POST", path, raw_body=body, token=token
        )
        self.assertEqual((status, response["code"]), (400, "invalid_request"))

        status, _, response = self.publish(token, playground_id, body, "  ")
        self.assertEqual((status, response["code"]), (400, "invalid_request"))

        status, _, response = self.publish(
            token, playground_id, body, "k" * 256
        )
        self.assertEqual((status, response["code"]), (400, "invalid_request"))

    def test_publish_rejects_oversized_request_before_reading(self):
        admin = self.seed_user("admin")
        token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(token, playground_id)
        path = f"/api/v1/playgrounds/{playground_id}/commits"

        # The declared length is over the limit while the sent body stays
        # small, so the server must answer 413 without consuming the stream.
        status, _, response = self.request(
            "POST",
            path,
            raw_body=b"x",
            token=token,
            extra_headers={
                "Idempotency-Key": self.unique("key"),
                "Content-Length": str(bundles.MAX_ARCHIVE_BYTES + 1),
            },
        )
        self.assertEqual(
            (status, response["code"]), (413, "request_too_large")
        )

    def test_publish_authorization(self):
        admin = self.seed_user("admin")
        student = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        student_token = self.login_token(student.username)
        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        body = self.build_zip()

        for token in (None, "bogus"):
            status, _, response = self.publish(
                token, playground_id, body, self.unique("key")
            )
            self.assertEqual(
                (status, response["code"]), (401, "invalid_session")
            )

        status, _, response = self.publish(
            student_token, playground_id, body, self.unique("key")
        )
        self.assertEqual((status, response["code"]), (403, "forbidden"))

        status, _, response = self.publish(
            admin_token, "missing-playground", body, self.unique("key")
        )
        self.assertEqual((status, response["code"]), (404, "not_found"))

        # Archived playgrounds cannot be published to by anyone.
        status, _, _ = self.request(
            "PATCH",
            f"/api/v1/playgrounds/{playground_id}",
            {"archived": True},
            token=admin_token,
        )
        self.assertEqual(status, 200)
        status, _, response = self.publish(
            admin_token, playground_id, body, self.unique("key")
        )
        self.assertEqual((status, response["code"]), (403, "forbidden"))

    def test_publish_cas_conflict_rolls_back_idempotency(self):
        admin = self.seed_user("admin")
        token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(token, playground_id)
        endpoint = f"POST /api/v1/playgrounds/{playground_id}/commits"

        with mock.patch.object(
            repository,
            "publish_playground",
            side_effect=repository.PlaygroundHeadChangedError(),
        ):
            status, _, response = self.publish(
                token, playground_id, self.build_zip(), "key-cas"
            )
        self.assertEqual(
            (status, response["code"]), (409, "playground_head_changed")
        )
        self.assertEqual(self.count_idempotency_rows(endpoint), 0)

        # The failed attempt left no key, so the same key can be retried.
        retry_status, _, retry = self.publish(
            token, playground_id, self.build_zip(), "key-cas"
        )
        self.assertEqual(retry_status, 201)
        self.assertFalse(retry["noOp"])

    def test_publish_never_extracts_archive_to_disk(self):
        admin = self.seed_user("admin")
        token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(token, playground_id)

        with mock.patch.object(
            zipfile.ZipFile, "extract", side_effect=AssertionError("extract")
        ), mock.patch.object(
            zipfile.ZipFile,
            "extractall",
            side_effect=AssertionError("extractall"),
        ):
            status, _, response = self.publish(
                token, playground_id, self.build_zip(), self.unique("key")
            )
        self.assertEqual(status, 201)
        self.assertTrue(response["commitId"])

    def test_playground_policy_archive_and_unarchive_authorization(self):
        admin = self.seed_user("admin")
        owner = self.seed_user("teacher")
        other = self.seed_user("teacher")
        student = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        owner_token = self.login_token(owner.username)
        other_token = self.login_token(other.username)
        student_token = self.login_token(student.username)

        playground_id = self.create_playground(
            admin_token, self.unique("pg")
        )["id"]

        # Only Admin may change policy or archive state.
        for token in (owner_token, other_token, student_token):
            for payload in ({"policy": "public"}, {"archived": True}):
                status, _, body = self.request(
                    "PATCH",
                    f"/api/v1/playgrounds/{playground_id}",
                    payload,
                    token=token,
                )
                self.assertEqual((status, body["code"]), (403, "forbidden"))

        status, _, body = self.request(
            "PATCH",
            f"/api/v1/playgrounds/{playground_id}",
            {"policy": "public"},
            token=admin_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["playground"]["policy"], "public")

        status, _, body = self.request(
            "PATCH",
            f"/api/v1/playgrounds/{playground_id}",
            {"policy": "release_only"},
            token=admin_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["playground"]["policy"], "release_only")

        # Admin archives; retrying keeps the first archivedAt.
        status, _, body = self.request(
            "PATCH",
            f"/api/v1/playgrounds/{playground_id}",
            {"archived": True},
            token=admin_token,
        )
        self.assertEqual(status, 200)
        archived_at = body["playground"]["archivedAt"]
        self.assertIsNotNone(archived_at)

        status, _, body = self.request(
            "PATCH",
            f"/api/v1/playgrounds/{playground_id}",
            {"archived": True},
            token=admin_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["playground"]["archivedAt"], archived_at)

        status, _, body = self.request(
            "PATCH",
            f"/api/v1/playgrounds/{playground_id}",
            {"archived": False},
            token=admin_token,
        )
        self.assertEqual(status, 200)
        self.assertIsNone(body["playground"]["archivedAt"])

        # Missing playgrounds are not found.
        for payload in ({"policy": "public"}, {"archived": True}):
            status, _, body = self.request(
                "PATCH",
                "/api/v1/playgrounds/missing-playground",
                payload,
                token=admin_token,
            )
            self.assertEqual((status, body["code"]), (404, "not_found"))

    def test_playground_creation_defaults_and_stores_no_content(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)
        playground_id = self.unique("pg")

        status, _, body = self.request(
            "POST",
            "/api/v1/playgrounds",
            {
                "id": playground_id,
                "policy": "public",
                "content": {"main.py": "print('hi')"},
                "bundle": "base64",
                "refs": ["refs/heads/main"],
                "git": {"commit": "deadbeef"},
            },
            token=admin_token,
        )
        self.assertEqual(status, 201)
        playground = body["playground"]
        self.assertEqual(
            set(playground),
            {"id", "creatorId", "policy", "createdAt", "archivedAt"},
        )
        self.assertEqual(playground["policy"], "release_only")
        self.assertIsNone(playground["archivedAt"])

        connection = open_database(self.database_path)
        try:
            rows = connection.execute(
                "SELECT creator_id, policy, archived_at FROM playgrounds"
                " WHERE id = ?",
                (playground_id,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["creator_id"], admin.id)
        self.assertEqual(rows[0]["policy"], "release_only")
        self.assertIsNone(rows[0]["archived_at"])

        # Metadata creation writes no Git refs or loose objects.
        self.assertEqual(
            list((self.repository_path / "refs" / "heads").iterdir()), []
        )
        self.assertEqual(
            list((self.repository_path / "refs" / "tags").iterdir()), []
        )
        for path in self.repository_path.rglob("*"):
            self.assertNotIn(playground_id, path.name)

    def test_playground_mutation_validation(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)

        for payload in ({}, {"id": ""}, {"id": 7}, {"id": None}):
            status, _, body = self.request(
                "POST", "/api/v1/playgrounds", payload, token=admin_token
            )
            self.assertEqual(
                (status, body["code"]), (400, "invalid_request"), payload
            )

        playground_id = self.create_playground(
            admin_token, self.unique("pg")
        )["id"]
        status, _, body = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": playground_id},
            token=admin_token,
        )
        self.assertEqual((status, body["code"]), (409, "conflict"))

        invalid_mutations = (
            {},
            {"policy": "public", "archived": True},
            {"policy": "secret"},
            {"policy": 7},
            {"policy": None},
            {"archived": "true"},
            {"archived": 1},
            {"archived": None},
            {"unknown": True},
            {"policy": "public", "extra": 1},
        )
        for payload in invalid_mutations:
            status, _, body = self.request(
                "PATCH",
                f"/api/v1/playgrounds/{playground_id}",
                payload,
                token=admin_token,
            )
            self.assertEqual(
                (status, body["code"]), (400, "invalid_request"), payload
            )

        # Rejected mutations left the stored metadata untouched.
        status, _, body = self.request(
            "PATCH",
            f"/api/v1/playgrounds/{playground_id}",
            {"policy": "public"},
            token=admin_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["playground"]["policy"], "public")

    def test_playground_routes_reject_invalid_sessions(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)
        playground = self.create_playground(admin_token, self.unique("pg"))

        calls = (
            ("POST", "/api/v1/playgrounds", {"id": "pg-unauthorized"}),
            (
                "PATCH",
                f"/api/v1/playgrounds/{playground['id']}",
                {"policy": "public"},
            ),
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

    def test_playground_route_methods_and_paths(self):
        method_not_allowed = (
            ("GET", "/api/v1/playgrounds", "POST"),
            ("PUT", "/api/v1/playgrounds", "POST"),
            ("PATCH", "/api/v1/playgrounds", "POST"),
            ("DELETE", "/api/v1/playgrounds", "POST"),
            ("GET", "/api/v1/playgrounds/some-id", "PATCH"),
            ("POST", "/api/v1/playgrounds/some-id", "PATCH"),
            ("PUT", "/api/v1/playgrounds/some-id", "PATCH"),
            ("DELETE", "/api/v1/playgrounds/some-id", "PATCH"),
            ("GET", "/api/v1/playgrounds/some-id/commits", "POST"),
            ("PUT", "/api/v1/playgrounds/some-id/commits", "POST"),
            ("PATCH", "/api/v1/playgrounds/some-id/commits", "POST"),
            ("DELETE", "/api/v1/playgrounds/some-id/commits", "POST"),
        )
        for method, path, allow in method_not_allowed:
            status, headers, body = self.request(method, path)
            self.assertEqual(status, 405, (method, path))
            self.assertEqual(headers.get("allow"), allow, (method, path))
            self.assertEqual(body["code"], "method_not_allowed")

        not_found = (
            ("GET", "/api/v1/playgrounds/"),
            ("GET", "/api/v1/playgrounds/a/b"),
            ("POST", "/api/v1/playgrounds/a/b/commits"),
            ("POST", "/api/v1/playgrounds/some-id/commits/extra"),
        )
        for method, path in not_found:
            status, _, body = self.request(method, path)
            self.assertEqual(
                (status, body["code"]), (404, "not_found"), (method, path)
            )


class ReleaseApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.database_path = Path(cls.temporary_directory.name) / "app.db"
        cls.repository_path = Path(cls.temporary_directory.name) / "picopg.git"
        cls.server = make_server(
            host="127.0.0.1",
            port=0,
            database_path=cls.database_path,
            repository_path=cls.repository_path,
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
        extra_headers=None,
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
        if extra_headers is not None:
            headers.update(extra_headers)

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

    def create_class(self, admin_token, teacher_id, name="Class"):
        status, _, body = self.request(
            "POST",
            "/api/v1/admin/classes",
            {"name": name, "teacherId": teacher_id},
            token=admin_token,
        )
        self.assertEqual(status, 201)
        return body["class"]

    def create_playground(self, token, playground_id):
        status, _, body = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": playground_id},
            token=token,
        )
        self.assertEqual(status, 201)
        return body["playground"]

    def build_zip(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "picopg.json", b'{"schemaVersion":1,"runtime":"python"}'
            )
            archive.writestr("course/module/app.py", b"print(1)")
        return buffer.getvalue()

    def publish(self, token, playground_id, key):
        return self.request(
            "POST",
            f"/api/v1/playgrounds/{playground_id}/commits",
            raw_body=self.build_zip(),
            token=token,
            content_type="application/zip",
            extra_headers={"Idempotency-Key": key},
        )

    def post_release(self, token, class_id, key, payload):
        headers = {} if key is None else {"Idempotency-Key": key}
        return self.request(
            "POST",
            f"/api/v1/classes/{class_id}/releases",
            payload,
            token=token,
            extra_headers=headers,
        )

    def release_payload(
        self,
        playground_id,
        commit_id,
        open_at="2020-01-01T00:00:00Z",
        due_at=None,
        requires_submission=False,
    ):
        payload = {
            "playgroundId": playground_id,
            "playgroundCommitId": commit_id,
            "openAt": open_at,
            "requiresSubmission": requires_submission,
        }
        if due_at is not None:
            payload["dueAt"] = due_at
        return payload

    def teacher_class_playground(self):
        """Return an owning teacher token, a Class, and a real commit."""
        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        admin_token = self.login_token(admin.username)
        teacher_token = self.login_token(teacher.username)
        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        _, _, published = self.publish(
            admin_token, playground_id, self.unique("key")
        )
        klass = self.create_class(admin_token, teacher.id)
        return admin_token, teacher_token, klass, playground_id, published[
            "commitId"
        ]

    def test_create_release_success_shape_and_exact_retry(self):
        admin_token, teacher_token, klass, playground_id, commit_id = (
            self.teacher_class_playground()
        )
        payload = self.release_payload(
            playground_id, commit_id, requires_submission=True
        )

        status, _, body = self.post_release(
            teacher_token, klass["id"], "key-create", payload
        )
        self.assertEqual(status, 201)
        release = body["release"]
        self.assertEqual(
            set(release),
            {
                "id",
                "classId",
                "playgroundId",
                "playgroundCommitId",
                "createdBy",
                "openAt",
                "dueAt",
                "requiresSubmission",
                "createdAt",
            },
        )
        self.assertEqual(release["classId"], klass["id"])
        self.assertEqual(release["playgroundId"], playground_id)
        self.assertEqual(release["playgroundCommitId"], commit_id)
        self.assertIs(release["requiresSubmission"], True)
        self.assertIsNone(release["dueAt"])
        self.assertEqual(release["openAt"], "2020-01-01T00:00:00.000000Z")
        # No source bundle content is echoed in the metadata response.
        self.assertNotIn("files", release)
        self.assertNotIn("content", release)

        retry_status, _, retry = self.post_release(
            teacher_token, klass["id"], "key-create", payload
        )
        self.assertEqual(retry_status, 201)
        self.assertEqual(retry, body)

        status, _, listed = self.request(
            "GET", "/api/v1/releases", token=admin_token
        )
        self.assertEqual(status, 200)
        matching = [
            item for item in listed["releases"] if item["id"] == release["id"]
        ]
        self.assertEqual(len(matching), 1)

    def test_due_date_and_null_due_date_serialization(self):
        _, teacher_token, klass, playground_id, commit_id = (
            self.teacher_class_playground()
        )
        with_due = self.release_payload(
            playground_id,
            commit_id,
            open_at="2026-01-01T00:00:00Z",
            due_at="2026-02-01T00:00:00Z",
        )
        status, _, body = self.post_release(
            teacher_token, klass["id"], "key-due", with_due
        )
        self.assertEqual(status, 201)
        self.assertEqual(
            body["release"]["dueAt"], "2026-02-01T00:00:00.000000Z"
        )

        without_due = self.release_payload(playground_id, commit_id)
        status, _, body = self.post_release(
            teacher_token, klass["id"], "key-nodue", without_due
        )
        self.assertEqual(status, 201)
        self.assertIsNone(body["release"]["dueAt"])

    def test_creation_authorization_by_role(self):
        admin_token, teacher_token, klass, playground_id, commit_id = (
            self.teacher_class_playground()
        )
        student = self.seed_user("student")
        other_teacher = self.seed_user("teacher")
        student_token = self.login_token(student.username)
        other_token = self.login_token(other_teacher.username)
        payload = self.release_payload(playground_id, commit_id)

        for token in (student_token, other_token, admin_token):
            status, _, body = self.post_release(
                token, klass["id"], self.unique("key"), payload
            )
            self.assertEqual((status, body["code"]), (403, "forbidden"))

    def test_wrong_class_and_commit_are_rejected(self):
        admin_token, teacher_token, klass, playground_id, commit_id = (
            self.teacher_class_playground()
        )
        other_teacher = self.seed_user("teacher")
        other_class = self.create_class(
            admin_token, other_teacher.id, "Other class"
        )

        status, _, body = self.post_release(
            teacher_token,
            "missing-class",
            "key-noclass",
            self.release_payload(playground_id, commit_id),
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

        # A class owned by another teacher is forbidden, not found.
        status, _, body = self.post_release(
            teacher_token,
            other_class["id"],
            "key-wrongclass",
            self.release_payload(playground_id, commit_id),
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))

        # A well-formed but foreign commit is not part of this history.
        status, _, body = self.post_release(
            teacher_token,
            klass["id"],
            "key-foreign",
            self.release_payload(playground_id, "f" * 40),
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

        # A malformed commit is an invalid request, not a lookup miss.
        status, _, body = self.post_release(
            teacher_token,
            klass["id"],
            "key-malformed",
            self.release_payload(playground_id, "not-a-commit"),
        )
        self.assertEqual((status, body["code"]), (400, "invalid_request"))

    def test_creation_request_validation(self):
        _, teacher_token, klass, playground_id, commit_id = (
            self.teacher_class_playground()
        )
        path = f"/api/v1/classes/{klass['id']}/releases"
        valid = self.release_payload(playground_id, commit_id)

        # Missing or blank keys are rejected before any body is read.
        for headers in ({}, {"Idempotency-Key": "  "}):
            status, _, body = self.request(
                "POST",
                path,
                valid,
                token=teacher_token,
                extra_headers=headers,
            )
            self.assertEqual((status, body["code"]), (400, "invalid_request"))

        status, _, body = self.post_release(
            teacher_token, klass["id"], "k" * 256, valid
        )
        self.assertEqual((status, body["code"]), (400, "invalid_request"))

        invalid_bodies = (
            {"playgroundCommitId": commit_id, "openAt": "2020-01-01T00:00:00Z"},
            {"playgroundId": playground_id, "openAt": "2020-01-01T00:00:00Z"},
            {"playgroundId": playground_id, "playgroundCommitId": commit_id},
            {
                "playgroundId": playground_id,
                "playgroundCommitId": commit_id,
                "openAt": "2020-01-01T00:00:00Z",
                "requiresSubmission": "true",
            },
            {
                "playgroundId": playground_id,
                "playgroundCommitId": commit_id,
                "openAt": "2020-01-01T00:00:00",
                "requiresSubmission": False,
            },
            {
                "playgroundId": playground_id,
                "playgroundCommitId": commit_id,
                "openAt": "2020-01-01T00:00:00Z",
                "dueAt": "2019-12-31T00:00:00Z",
                "requiresSubmission": False,
            },
        )
        for invalid in invalid_bodies:
            status, _, body = self.post_release(
                teacher_token, klass["id"], self.unique("key"), invalid
            )
            self.assertEqual(
                (status, body["code"]), (400, "invalid_request"), invalid
            )

        status, _, body = self.post_release(
            teacher_token, klass["id"], self.unique("key"), {}
        )
        self.assertEqual((status, body["code"]), (400, "invalid_request"))

    def test_idempotency_conflict_on_changed_body(self):
        _, teacher_token, klass, playground_id, commit_id = (
            self.teacher_class_playground()
        )
        first = self.release_payload(
            playground_id, commit_id, open_at="2020-01-01T00:00:00Z"
        )
        status, _, _ = self.post_release(
            teacher_token, klass["id"], "key-conflict", first
        )
        self.assertEqual(status, 201)

        changed = self.release_payload(
            playground_id, commit_id, open_at="2021-01-01T00:00:00Z"
        )
        status, _, body = self.post_release(
            teacher_token, klass["id"], "key-conflict", changed
        )
        self.assertEqual(
            (status, body["code"]), (409, "idempotency_conflict")
        )

        status, _, listed = self.request(
            "GET", "/api/v1/releases", token=teacher_token
        )
        matching = [
            item
            for item in listed["releases"]
            if item["classId"] == klass["id"]
        ]
        self.assertEqual(len(matching), 1)

    def test_unauthenticated_calls_are_rejected_first(self):
        _, _, klass, playground_id, commit_id = self.teacher_class_playground()
        valid = self.release_payload(playground_id, commit_id)
        calls = (
            self.request(
                "POST",
                f"/api/v1/classes/{klass['id']}/releases",
                valid,
                extra_headers={"Idempotency-Key": "key"},
            ),
            self.request(
                "POST",
                f"/api/v1/classes/{klass['id']}/releases",
                valid,
                token="bogus",
                extra_headers={"Idempotency-Key": "key"},
            ),
            self.request("GET", "/api/v1/releases"),
            self.request("GET", "/api/v1/releases/missing-release"),
        )
        for status, _, body in calls:
            self.assertEqual((status, body["code"]), (401, "invalid_session"))

    def test_visibility_and_hidden_reads(self):
        admin_token, teacher_token, klass, playground_id, commit_id = (
            self.teacher_class_playground()
        )
        student = self.seed_user("student")
        outsider = self.seed_user("student")
        student_token = self.login_token(student.username)
        outsider_token = self.login_token(outsider.username)
        status, _, _ = self.request(
            "PUT",
            f"/api/v1/admin/classes/{klass['id']}/members/{student.id}",
            token=admin_token,
        )
        self.assertEqual(status, 200)

        open_release = self.post_release(
            teacher_token,
            klass["id"],
            "key-open",
            self.release_payload(playground_id, commit_id),
        )[2]["release"]
        future_release = self.post_release(
            teacher_token,
            klass["id"],
            "key-future",
            self.release_payload(
                playground_id, commit_id, open_at="2999-01-01T00:00:00Z"
            ),
        )[2]["release"]

        status, _, body = self.request(
            "GET", "/api/v1/releases", token=student_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in body["releases"]], [open_release["id"]]
        )

        status, _, body = self.request(
            "GET",
            f"/api/v1/releases/{future_release['id']}",
            token=student_token,
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

        status, _, body = self.request(
            "GET",
            f"/api/v1/releases/{open_release['id']}",
            token=student_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["release"]["id"], open_release["id"])

        # The owning Teacher and Admin see the future Release.
        for token in (teacher_token, admin_token):
            status, _, listing = self.request(
                "GET", "/api/v1/releases", token=token
            )
            ids = [item["id"] for item in listing["releases"]]
            self.assertIn(future_release["id"], ids)
            status, _, body = self.request(
                "GET", f"/api/v1/releases/{future_release['id']}", token=token
            )
            self.assertEqual(status, 200)

        # An unrelated student cannot see or read the open Release.
        status, _, body = self.request(
            "GET", "/api/v1/releases", token=outsider_token
        )
        self.assertEqual(body["releases"], [])
        status, _, body = self.request(
            "GET", f"/api/v1/releases/{open_release['id']}", token=outsider_token
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

        status, _, body = self.request(
            "GET", "/api/v1/releases/does-not-exist", token=admin_token
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

    def test_release_route_methods_and_allow_headers(self):
        _, _, klass, _, _ = self.teacher_class_playground()
        method_not_allowed = (
            ("GET", f"/api/v1/classes/{klass['id']}/releases", "POST"),
            ("PUT", f"/api/v1/classes/{klass['id']}/releases", "POST"),
            ("PATCH", f"/api/v1/classes/{klass['id']}/releases", "POST"),
            ("DELETE", f"/api/v1/classes/{klass['id']}/releases", "POST"),
            ("POST", "/api/v1/releases", "GET, HEAD"),
            ("PUT", "/api/v1/releases", "GET, HEAD"),
            ("PATCH", "/api/v1/releases", "GET, HEAD"),
            ("DELETE", "/api/v1/releases", "GET, HEAD"),
            ("POST", "/api/v1/releases/some-id", "GET, HEAD"),
            ("PUT", "/api/v1/releases/some-id", "GET, HEAD"),
            ("PATCH", "/api/v1/releases/some-id", "GET, HEAD"),
            ("DELETE", "/api/v1/releases/some-id", "GET, HEAD"),
        )
        for method, path, allow in method_not_allowed:
            status, headers, body = self.request(method, path)
            self.assertEqual(status, 405, (method, path))
            self.assertEqual(headers.get("allow"), allow, (method, path))
            self.assertEqual(body["code"], "method_not_allowed")

        not_found = (
            ("GET", "/api/v1/releases/"),
            ("GET", "/api/v1/releases/a/b"),
            ("POST", "/api/v1/classes//releases"),
            ("POST", "/api/v1/classes/some-id/releases/extra"),
        )
        for method, path in not_found:
            status, _, body = self.request(method, path)
            self.assertEqual(
                (status, body["code"]), (404, "not_found"), (method, path)
            )


class ContentApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.database_path = Path(cls.temporary_directory.name) / "app.db"
        cls.repository_path = Path(cls.temporary_directory.name) / "picopg.git"
        cls.server = make_server(
            host="127.0.0.1",
            port=0,
            database_path=cls.database_path,
            repository_path=cls.repository_path,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temporary_directory.cleanup()

    MANIFEST = b'{"schemaVersion":1,"runtime":"python"}'
    BINARY = bytes(range(256))

    def request(
        self,
        method,
        path,
        payload=None,
        token=None,
        raw_body=None,
        content_type="application/json",
        extra_headers=None,
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
        if extra_headers is not None:
            headers.update(extra_headers)

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

    def create_class(self, admin_token, teacher_id):
        status, _, body = self.request(
            "POST",
            "/api/v1/admin/classes",
            {"name": self.unique("Class"), "teacherId": teacher_id},
            token=admin_token,
        )
        self.assertEqual(status, 201)
        return body["class"]

    def create_playground(self, token, playground_id):
        status, _, body = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": playground_id},
            token=token,
        )
        self.assertEqual(status, 201)
        return body["playground"]

    def set_policy(self, token, playground_id, policy):
        status, _, body = self.request(
            "PATCH",
            f"/api/v1/playgrounds/{playground_id}",
            {"policy": policy},
            token=token,
        )
        self.assertEqual(status, 200)
        return body["playground"]

    def assign_student(self, admin_token, class_id, student_id):
        status, _, _ = self.request(
            "PUT",
            f"/api/v1/admin/classes/{class_id}/members/{student_id}",
            token=admin_token,
        )
        self.assertEqual(status, 200)

    def build_zip(self, entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return buffer.getvalue()

    def default_entries(self):
        return (
            ("picopg.json", self.MANIFEST),
            ("course/module/app.py", b"print(1)"),
            ("course/data.bin", self.BINARY),
        )

    def publish(self, token, playground_id, entries=None, key=None):
        if entries is None:
            entries = self.default_entries()
        return self.request(
            "POST",
            f"/api/v1/playgrounds/{playground_id}/commits",
            raw_body=self.build_zip(entries),
            token=token,
            content_type="application/zip",
            extra_headers={"Idempotency-Key": key or self.unique("key")},
        )

    def post_release(
        self, token, class_id, playground_id, commit_id, open_at
    ):
        return self.request(
            "POST",
            f"/api/v1/classes/{class_id}/releases",
            {
                "playgroundId": playground_id,
                "playgroundCommitId": commit_id,
                "openAt": open_at,
                "requiresSubmission": False,
            },
            token=token,
            extra_headers={"Idempotency-Key": self.unique("key")},
        )

    def release(self, token, class_id, playground_id, commit_id, open_at):
        status, _, body = self.post_release(
            token, class_id, playground_id, commit_id, open_at
        )
        self.assertEqual(status, 201)
        return body["release"]

    def manifest_path(self, playground_id, commit_id):
        return (
            f"/api/v1/playgrounds/{playground_id}/commits/{commit_id}/manifest"
        )

    def file_path(self, playground_id, commit_id, path):
        return (
            f"/api/v1/playgrounds/{playground_id}"
            f"/commits/{commit_id}/files/{path}"
        )

    # ----- public downloads ----------------------------------------------

    def test_anonymous_public_manifest_and_file_download(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        self.set_policy(admin_token, playground_id, "public")
        _, _, published = self.publish(admin_token, playground_id)
        commit_id = published["commitId"]

        status, _, body = self.request(
            "GET", self.manifest_path(playground_id, commit_id)
        )
        self.assertEqual(status, 200)
        manifest = body["manifest"]
        self.assertEqual(manifest["commitId"], commit_id)
        files = {item["path"]: item for item in manifest["files"]}
        self.assertEqual(
            sorted(files),
            ["course/data.bin", "course/module/app.py", "picopg.json"],
        )
        expected = {
            "course/module/app.py": b"print(1)",
            "course/data.bin": self.BINARY,
            "picopg.json": self.MANIFEST,
        }
        for path, data in expected.items():
            self.assertEqual(files[path]["size"], len(data))
            self.assertEqual(
                files[path]["sha256"], hashlib.sha256(data).hexdigest()
            )

        status, headers, raw = self._raw(
            "GET", self.file_path(playground_id, commit_id, "course/data.bin")
        )
        self.assertEqual(status, 200)
        self.assertEqual(raw, self.BINARY)
        self.assertEqual(headers["content-type"], "application/octet-stream")
        self.assertEqual(headers["content-length"], str(len(self.BINARY)))
        self.assertEqual(
            headers["etag"], f'"{hashlib.sha256(self.BINARY).hexdigest()}"'
        )
        self.assertEqual(headers["x-content-type-options"], "nosniff")

        status, headers, raw = self._raw(
            "GET",
            self.file_path(playground_id, commit_id, "course/module/app.py"),
        )
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"print(1)")
        self.assertEqual(
            headers["etag"],
            f'"{hashlib.sha256(b"print(1)").hexdigest()}"',
        )

    def test_file_bytes_and_head_headers_match(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        self.set_policy(admin_token, playground_id, "public")
        _, _, published = self.publish(admin_token, playground_id)
        commit_id = published["commitId"]
        path = self.file_path(playground_id, commit_id, "course/data.bin")

        get_status, get_headers, get_body = self._raw("GET", path)
        head_status, head_headers, head_body = self._raw("HEAD", path)

        self.assertEqual(get_status, 200)
        self.assertEqual(head_status, 200)
        self.assertEqual(head_body, b"")
        self.assertEqual(get_body, self.BINARY)
        for name in (
            "content-type",
            "content-length",
            "etag",
            "x-content-type-options",
        ):
            self.assertEqual(get_headers[name], head_headers[name], name)
        self.assertEqual(
            get_headers["etag"],
            f'"{hashlib.sha256(self.BINARY).hexdigest()}"',
        )
        self.assertEqual(
            head_headers["content-length"], str(len(self.BINARY))
        )

        manifest = self.manifest_path(playground_id, commit_id)
        get_status, get_headers, get_body = self._raw("GET", manifest)
        head_status, head_headers, head_body = self._raw("HEAD", manifest)
        self.assertEqual((get_status, head_status), (200, 200))
        self.assertEqual(head_body, b"")
        self.assertEqual(
            get_headers["content-length"],
            head_headers["content-length"],
        )

    def _raw(self, method, path, token=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        status = response.status
        response_headers = {
            key.lower(): value for key, value in response.getheaders()
        }
        connection.close()
        return status, response_headers, body

    # ----- catalog -------------------------------------------------------

    def test_catalog_public_release_dedup_and_order(self):
        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        student = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        teacher_token = self.login_token(teacher.username)
        student_token = self.login_token(student.username)

        public_id = self.unique("pg")
        released_id = self.unique("pg")
        self.create_playground(admin_token, public_id)
        self.create_playground(admin_token, released_id)
        self.set_policy(admin_token, public_id, "public")
        public_commit = self.publish(admin_token, public_id)[2][
            "commitId"
        ]
        released_commit = self.publish(admin_token, released_id)[2][
            "commitId"
        ]

        klass = self.create_class(admin_token, teacher.id)
        self.assign_student(admin_token, klass["id"], student.id)
        release_status, _, release_body = self.post_release(
            teacher_token,
            klass["id"],
            released_id,
            released_commit,
            "2020-01-01T00:00:00Z",
        )
        self.assertEqual(release_status, 201)
        release_id = release_body["release"]["id"]

        # The released Playground is also public, so the student's catalog
        # must merge the two sources into one deduplicated item.
        self.set_policy(admin_token, released_id, "public")

        status, _, body = self.request(
            "GET", "/api/v1/catalog", token=student_token
        )
        self.assertEqual(status, 200)
        entries = body["catalog"]
        keys = [(item["playgroundId"], item["commitId"]) for item in entries]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len(set(keys)))
        merged = [
            item
            for item in entries
            if item["playgroundId"] == released_id
        ]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["commitId"], released_commit)
        self.assertEqual(merged[0]["releaseIds"], [release_id])
        self.assertIn((public_id, public_commit), keys)

        # An anonymous catalog still lists public items, but no Release ids.
        status, _, body = self.request("GET", "/api/v1/catalog")
        self.assertEqual(status, 200)
        anonymous_entries = {
            (item["playgroundId"], item["commitId"]): item["releaseIds"]
            for item in body["catalog"]
        }
        self.assertEqual(anonymous_entries[(released_id, released_commit)], [])
        self.assertIn((public_id, public_commit), anonymous_entries)

    def test_catalog_excludes_archived_foreign_and_future(self):
        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        student = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        teacher_token = self.login_token(teacher.username)
        student_token = self.login_token(student.username)

        archived_id = self.unique("pg")
        self.create_playground(admin_token, archived_id)
        self.set_policy(admin_token, archived_id, "public")
        archived_commit = self.publish(admin_token, archived_id)[2][
            "commitId"
        ]
        self.request(
            "PATCH",
            f"/api/v1/playgrounds/{archived_id}",
            {"archived": True},
            token=admin_token,
        )

        future_id = self.unique("pg")
        self.create_playground(admin_token, future_id)
        future_commit = self.publish(admin_token, future_id)[2]["commitId"]
        klass = self.create_class(admin_token, teacher.id)
        self.assign_student(admin_token, klass["id"], student.id)
        self.release(
            teacher_token,
            klass["id"],
            future_id,
            future_commit,
            "2999-01-01T00:00:00Z",
        )

        # Archived public content is never in an anonymous catalog.
        status, _, body = self.request("GET", "/api/v1/catalog")
        self.assertEqual(status, 200)
        public_keys = {
            (item["playgroundId"], item["commitId"])
            for item in body["catalog"]
        }
        self.assertNotIn((archived_id, archived_commit), public_keys)

        # A future Release is not yet authorized for the student.
        status, _, body = self.request(
            "GET", "/api/v1/catalog", token=student_token
        )
        self.assertEqual(status, 200)
        student_keys = {
            (item["playgroundId"], item["commitId"])
            for item in body["catalog"]
        }
        self.assertNotIn((future_id, future_commit), student_keys)

        # Admin may read every Release, including a future one.
        status, _, body = self.request(
            "GET", "/api/v1/catalog", token=admin_token
        )
        self.assertEqual(status, 200)
        admin_keys = {
            (item["playgroundId"], item["commitId"])
            for item in body["catalog"]
        }
        self.assertIn((future_id, future_commit), admin_keys)

        # A transferred student loses the old Class Release from the catalog.
        other_teacher = self.seed_user("teacher")
        other_class = self.create_class(admin_token, other_teacher.id)
        open_id = self.unique("pg")
        self.create_playground(admin_token, open_id)
        open_commit = self.publish(admin_token, open_id)[2]["commitId"]
        self.release(
            teacher_token,
            klass["id"],
            open_id,
            open_commit,
            "2020-01-01T00:00:00Z",
        )
        status, _, body = self.request(
            "GET", "/api/v1/catalog", token=student_token
        )
        self.assertIn(
            (open_id, open_commit),
            {
                (item["playgroundId"], item["commitId"])
                for item in body["catalog"]
            },
        )
        self.assign_student(admin_token, other_class["id"], student.id)
        status, _, body = self.request(
            "GET", "/api/v1/catalog", token=student_token
        )
        self.assertNotIn(
            (open_id, open_commit),
            {
                (item["playgroundId"], item["commitId"])
                for item in body["catalog"]
            },
        )

    # ----- authorization -------------------------------------------------

    def test_manifest_and_file_authorization_by_role(self):
        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        other_teacher = self.seed_user("teacher")
        student = self.seed_user("student")
        outsider = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        teacher_token = self.login_token(teacher.username)
        other_token = self.login_token(other_teacher.username)
        student_token = self.login_token(student.username)
        outsider_token = self.login_token(outsider.username)

        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        first_commit = self.publish(admin_token, playground_id)[2][
            "commitId"
        ]
        second_commit = self.publish(
            admin_token,
            playground_id,
            entries=(
                ("picopg.json", self.MANIFEST),
                ("course/module/app.py", b"print(2)"),
                ("course/data.bin", self.BINARY),
            ),
        )[2]["commitId"]
        self.assertNotEqual(first_commit, second_commit)
        klass = self.create_class(admin_token, teacher.id)
        self.assign_student(admin_token, klass["id"], student.id)
        self.release(
            teacher_token,
            klass["id"],
            playground_id,
            first_commit,
            "2020-01-01T00:00:00Z",
        )

        manifest = self.manifest_path(playground_id, first_commit)
        file_url = self.file_path(
            playground_id, first_commit, "course/module/app.py"
        )

        allowed = (teacher_token, student_token, admin_token)
        for token in allowed:
            status, _, _ = self.request("GET", manifest, token=token)
            self.assertEqual(status, 200)
            status, _, body = self._raw("GET", file_url, token=token)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"print(1)")

        denied = (None, outsider_token, other_token)
        for token in denied:
            status, _, body = self.request("GET", manifest, token=token)
            self.assertEqual((status, body["code"]), (404, "not_found"))
            status, _, body = self.request("GET", file_url, token=token)
            self.assertEqual((status, body["code"]), (404, "not_found"))

        # The Release pins the first commit: HEAD must not substitute for it.
        status, _, _ = self.request("GET", manifest, token=student_token)
        self.assertEqual(status, 200)
        status, _, body = self.request(
            "GET",
            self.manifest_path(playground_id, second_commit),
            token=student_token,
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

        # A foreign commit id and a nonexistent commit are both not found.
        status, _, body = self.request(
            "GET",
            self.manifest_path(playground_id, "a" * 40),
            token=student_token,
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))
        status, _, body = self.request(
            "GET",
            self.manifest_path(playground_id, "not-a-commit"),
            token=student_token,
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

    def test_future_release_denies_until_open(self):
        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        student = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        teacher_token = self.login_token(teacher.username)
        student_token = self.login_token(student.username)

        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        commit_id = self.publish(admin_token, playground_id)[2]["commitId"]
        klass = self.create_class(admin_token, teacher.id)
        self.assign_student(admin_token, klass["id"], student.id)
        self.release(
            teacher_token,
            klass["id"],
            playground_id,
            commit_id,
            "2999-01-01T00:00:00Z",
        )

        status, _, body = self.request(
            "GET", self.manifest_path(playground_id, commit_id),
            token=student_token,
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))
        status, _, body = self.request(
            "GET",
            self.file_path(playground_id, commit_id, "picopg.json"),
            token=student_token,
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

    def test_missing_and_denied_private_are_identical(self):
        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        admin_token = self.login_token(admin.username)
        teacher_token = self.login_token(teacher.username)

        private_id = self.unique("pg")
        self.create_playground(admin_token, private_id)
        commit_id = self.publish(admin_token, private_id)[2]["commitId"]

        denied = self.request(
            "GET", self.manifest_path(private_id, commit_id)
        )
        missing = self.request(
            "GET", self.manifest_path("pg-does-not-exist", "a" * 40)
        )
        self.assertEqual(denied[0], 404)
        self.assertEqual(missing[0], 404)
        self.assertEqual(denied[2], missing[2])

        denied = self.request(
            "GET", self.file_path(private_id, commit_id, "picopg.json")
        )
        missing = self.request(
            "GET",
            self.file_path("pg-does-not-exist", "a" * 40, "picopg.json"),
        )
        self.assertEqual(denied[0], 404)
        self.assertEqual(missing[0], 404)
        self.assertEqual(denied[2], missing[2])

    def test_invalid_supplied_token_returns_401(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        self.set_policy(admin_token, playground_id, "public")
        commit_id = self.publish(admin_token, playground_id)[2]["commitId"]

        paths = (
            "/api/v1/catalog",
            self.manifest_path(playground_id, commit_id),
            self.file_path(playground_id, commit_id, "picopg.json"),
        )
        for path in paths:
            status, _, body = self.request("GET", path, token="bogus")
            self.assertEqual((status, body["code"]), (401, "invalid_session"))

            connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
            connection.request(
                "GET", path, headers={"Authorization": "Basic abc"}
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 401, path)
            connection.close()

    # ----- paths ---------------------------------------------------------

    def test_path_resolution_rejects_traversal_and_foreign_paths(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        self.set_policy(admin_token, playground_id, "public")
        first = self.publish(
            admin_token,
            playground_id,
            entries=(
                ("picopg.json", self.MANIFEST),
                ("course/first.py", b"first"),
            ),
        )[2]["commitId"]
        second = self.publish(
            admin_token,
            playground_id,
            entries=(
                ("picopg.json", self.MANIFEST),
                ("course/second.py", b"second"),
            ),
        )[2]["commitId"]

        bad_paths = (
            "course/missing.py",
            "course/../picopg.json",
            "course/%2e%2e/picopg.json",
            "%2e%2e/picopg.json",
            "%252e%252e%252fpicopg.json",
            "course%2Fsecond.py",
            "course%5Csecond.py",
            "%2Fetc%2Fpasswd",
            "secret.txt",
            "work/picopg.json",
            ".picopg/work.json",
            "%FF",
        )
        for path in bad_paths:
            with self.subTest(path=path):
                status, _, body = self.request(
                    "GET", self.file_path(playground_id, second, path)
                )
                self.assertEqual(
                    (status, body.get("code")), (404, "not_found"), path
                )

        # A path that existed only in the first commit is not found in the
        # exact second commit, even though the first is still reachable.
        status, _, body = self._raw(
            "GET", self.file_path(playground_id, first, "course/first.py")
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"first")
        status, _, body = self.request(
            "GET", self.file_path(playground_id, second, "course/first.py")
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

    def test_route_methods_and_paths(self):
        method_not_allowed = (
            ("POST", "/api/v1/catalog", "GET, HEAD"),
            ("PUT", "/api/v1/catalog", "GET, HEAD"),
            ("PATCH", "/api/v1/catalog", "GET, HEAD"),
            ("DELETE", "/api/v1/catalog", "GET, HEAD"),
            (
                "POST",
                "/api/v1/playgrounds/p/commits/" + "a" * 40 + "/manifest",
                "GET, HEAD",
            ),
            (
                "DELETE",
                "/api/v1/playgrounds/p/commits/" + "a" * 40 + "/files/x",
                "GET, HEAD",
            ),
        )
        for method, path, allow in method_not_allowed:
            status, headers, body = self.request(method, path)
            self.assertEqual(status, 405, (method, path))
            self.assertEqual(headers.get("allow"), allow, (method, path))
            self.assertEqual(body["code"], "method_not_allowed")

        for path in (
            "/api/v1/catalog/",
            "/api/v1/catalog/extra",
            "/api/v1/playgrounds/p/commits/c/files",
            "/api/v1/playgrounds/p/commits/c/files/",
            "/api/v1/playgrounds/p/commits/c/manifest/extra",
            "/api/v1/playgrounds/p/commits/c/other",
            "/api/v1/playgrounds//commits/c/manifest",
        ):
            status, _, body = self.request("GET", path)
            self.assertEqual(
                (status, body["code"]), (404, "not_found"), path
            )


class GeneratedCacheApiTest(unittest.TestCase):
    """HTTP invariants for the injected artifact and generated directories."""

    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        base = Path(cls.temporary_directory.name)
        cls.database_path = base / "app.db"
        cls.repository_path = base / "picopg.git"
        cls.artifacts_path = base / "artifacts"
        cls.generated_path = base / "generated"
        cls.server = make_server(
            host="127.0.0.1",
            port=0,
            database_path=cls.database_path,
            repository_path=cls.repository_path,
            artifacts_path=cls.artifacts_path,
            generated_path=cls.generated_path,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temporary_directory.cleanup()

    MANIFEST = b'{"schemaVersion":1,"runtime":"python"}'

    def request(self, method, path, payload=None, token=None, raw_body=None,
                content_type="application/json", extra_headers=None):
        headers = {}
        body = raw_body
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            headers["Content-Type"] = content_type
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if extra_headers is not None:
            headers.update(extra_headers)

        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        status = response.status
        decoded = json.loads(response_body) if response_body else None
        connection.close()
        return status, decoded

    def seed_admin_token(self):
        username = f"admin-{uuid.uuid4().hex[:8]}"
        connection = open_database(self.database_path)
        try:
            with connection:
                accounts.create_user(
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
        status, body = self.request(
            "POST",
            "/api/v1/login",
            {"username": username, "password": "secret"},
        )
        self.assertEqual(status, 200)
        return body["sessionToken"]

    def build_zip(self, entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return buffer.getvalue()

    def test_rebuild_uses_injected_dirs_and_cache_is_not_authority(self):
        self.assertEqual(self.server.artifacts_path, self.artifacts_path)
        self.assertEqual(self.server.generated_path, self.generated_path)

        token = self.seed_admin_token()
        playground_id = f"private-{uuid.uuid4().hex[:8]}"
        status, _ = self.request(
            "POST", "/api/v1/playgrounds", {"id": playground_id}, token
        )
        self.assertEqual(status, 201)
        status, body = self.request(
            "POST",
            f"/api/v1/playgrounds/{playground_id}/commits",
            token=token,
            raw_body=self.build_zip([("picopg.json", self.MANIFEST)]),
            content_type="application/zip",
            extra_headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        self.assertEqual(status, 201)
        commit_id = body["commitId"]

        (self.artifacts_path / "v1").mkdir(parents=True)
        (self.artifacts_path / "v1" / "runtime.exe").write_bytes(b"runtime")
        connection = open_database(self.database_path)
        try:
            generated.rebuild(
                self.generated_path,
                connection=connection,
                repository=self.server.repository,
                artifacts_path=self.artifacts_path,
            )
        finally:
            connection.close()

        self.assertEqual(
            sorted(path.name for path in self.generated_path.iterdir()),
            sorted(generated.GENERATED_FILENAMES),
        )

        # A forged cache that advertises the private commit must not change
        # live authorization: the anonymous catalog stays empty and the
        # anonymous manifest read stays an indistinguishable not-found.
        generated.atomic_write(
            self.generated_path / generated.CATALOG_FILENAME,
            json.dumps(
                {
                    "catalog": [
                        {
                            "playgroundId": playground_id,
                            "commitId": commit_id,
                        }
                    ]
                }
            ).encode("utf-8"),
        )

        status, body = self.request("GET", "/api/v1/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(body["catalog"], [])

        status, body = self.request(
            "GET",
            f"/api/v1/playgrounds/{playground_id}"
            f"/commits/{commit_id}/manifest",
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))


class WorkSyncApiTest(unittest.TestCase):
    """HTTP contract for the bounded Work synchronization endpoint."""

    MANIFEST = b'{"schemaVersion":1,"runtime":"python"}'

    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        base = Path(cls.temporary_directory.name)
        cls.database_path = base / "app.db"
        cls.repository_path = base / "picopg.git"
        cls.server = make_server(
            host="127.0.0.1",
            port=0,
            database_path=cls.database_path,
            repository_path=cls.repository_path,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
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
        extra_headers=None,
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
        if extra_headers is not None:
            headers.update(extra_headers)

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

    def create_playground(self, token, playground_id):
        status, _, body = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": playground_id},
            token=token,
        )
        self.assertEqual(status, 201)
        return body["playground"]

    def set_policy(self, token, playground_id, policy):
        status, _, body = self.request(
            "PATCH",
            f"/api/v1/playgrounds/{playground_id}",
            {"policy": policy},
            token=token,
        )
        self.assertEqual(status, 200)
        return body["playground"]

    def publish(self, token, playground_id, course_data=b"print(1)"):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("picopg.json", self.MANIFEST)
            archive.writestr("course/module/app.py", course_data)
        status, _, body = self.request(
            "POST",
            f"/api/v1/playgrounds/{playground_id}/commits",
            raw_body=buffer.getvalue(),
            token=token,
            content_type="application/zip",
            extra_headers={"Idempotency-Key": self.unique("key")},
        )
        self.assertEqual(status, 201)
        return body["commitId"]

    def sync_path(self, playground_id):
        return f"/api/v1/works/{playground_id}/sync"

    @staticmethod
    def files(mapping):
        return [
            {"path": path, "data": base64.b64encode(data).decode("ascii")}
            for path, data in mapping.items()
        ]

    def sync(
        self, token, playground_id, playground_commit_id, mapping, base=None
    ):
        return self.request(
            "POST",
            self.sync_path(playground_id),
            {
                "playgroundCommitId": playground_commit_id,
                "baseCommitId": base,
                "files": self.files(mapping),
            },
            token=token,
        )

    def public_playground_as_admin(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)
        student = self.seed_user("student")
        student_token = self.login_token(student.username)
        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        self.set_policy(admin_token, playground_id, "public")
        commit_id = self.publish(admin_token, playground_id)
        return (
            admin_token,
            student,
            student_token,
            playground_id,
            commit_id,
        )

    def work_head(self, playground_id, student_id):
        return repository.resolve_work_head(
            self.server.repository, playground_id, student_id
        )

    # ----- success and CAS -------------------------------------------------

    def test_initial_and_next_sync_status_and_shape(self):
        _, student, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )

        status, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"print('a')"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["playgroundCommitId"], commit_id)
        first = body["workCommitId"]
        self.assertTrue(first)
        self.assertEqual(
            self.work_head(playground_id, student.id), first
        )

        status, _, body = self.sync(
            token,
            playground_id,
            commit_id,
            {"work/main.py": b"print('b')", "work/pkg/util.py": b"u"},
            base=first,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["playgroundCommitId"], commit_id)
        second = body["workCommitId"]
        self.assertNotEqual(second, first)
        self.assertEqual(
            self.work_head(playground_id, student.id), second
        )

        # The assembled tree keeps the pinned Playground content beside the
        # submitted Work snapshot and the Server-owned Work manifest.
        tree = {
            entry.path
            for entry in repository.list_commit_tree(
                self.server.repository, second
            )
        }
        self.assertEqual(
            tree,
            {
                ".picopg/work.json",
                "course/module/app.py",
                "picopg.json",
                "work/main.py",
                "work/pkg/util.py",
            },
        )

    def test_exact_retry_after_success_conflicts_with_canonical_head(self):
        _, _, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        status, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"a"}
        )
        self.assertEqual(status, 201)
        first = body["workCommitId"]

        # Resending the applied first request conflicts instead of being
        # treated as an idempotent replay.
        status, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"a"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "work_head_changed")
        self.assertEqual(body["currentHeadCommitId"], first)

        status, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"b"}, base=first
        )
        self.assertEqual(status, 200)
        second = body["workCommitId"]

        status, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"b"}, base=first
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "work_head_changed")
        self.assertEqual(body["currentHeadCommitId"], second)

    def test_two_clients_race_on_one_stale_base(self):
        _, _, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        status, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"a"}
        )
        self.assertEqual(status, 201)
        base = body["workCommitId"]

        status, _, body = self.sync(
            token,
            playground_id,
            commit_id,
            {"work/main.py": b"client-a"},
            base=base,
        )
        self.assertEqual(status, 200)
        winner = body["workCommitId"]

        status, _, body = self.sync(
            token,
            playground_id,
            commit_id,
            {"work/main.py": b"client-b"},
            base=base,
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["currentHeadCommitId"], winner)

        # The loser recovers by retrying against the reported HEAD.
        status, _, body = self.sync(
            token,
            playground_id,
            commit_id,
            {"work/main.py": b"client-b"},
            base=winner,
        )
        self.assertEqual(status, 200)

    # ----- rejected bodies -------------------------------------------------

    def test_malformed_and_duplicate_files_are_rejected(self):
        _, student, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        path = self.sync_path(playground_id)
        cases = [
            (
                {
                    "playgroundCommitId": commit_id,
                    "baseCommitId": None,
                    "files": [{"path": "work/a.py", "data": "!!!!"}],
                },
                "invalid_base64",
            ),
            (
                {
                    "playgroundCommitId": commit_id,
                    "baseCommitId": None,
                    "files": [
                        {"path": "work/a.py", "data": "MQ=="},
                        {"path": "work/a.py", "data": "Mg=="},
                    ],
                },
                "duplicate_path",
            ),
            (
                {
                    "playgroundCommitId": commit_id,
                    "baseCommitId": None,
                    "files": [{"path": "course/x.py", "data": "MQ=="}],
                },
                "path_not_allowed",
            ),
            (
                {
                    "playgroundCommitId": commit_id,
                    "baseCommitId": None,
                    "files": {"work/a.py": "MQ=="},
                },
                "invalid_files",
            ),
            (
                {
                    "playgroundCommitId": commit_id,
                    "files": [{"path": "work/a.py", "data": "MQ=="}],
                },
                "invalid_payload",
            ),
            (
                {
                    "playgroundCommitId": commit_id,
                    "baseCommitId": None,
                    "files": ["not-an-entry"],
                },
                "invalid_entry",
            ),
        ]
        for payload, reason in cases:
            with self.subTest(reason=reason):
                status, _, body = self.request(
                    "POST", path, payload, token=token
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["code"], "invalid_request")
                self.assertEqual(body["details"]["reason"], reason)

        # No rejected request partially created a Work commit.
        self.assertIsNone(self.work_head(playground_id, student.id))

    def test_work_limits_are_rejected_without_writing_a_commit(self):
        _, student, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        with mock.patch.object(works, "MAX_WORK_ENTRY_COUNT", 1):
            status, _, body = self.sync(
                token,
                playground_id,
                commit_id,
                {"work/a.py": b"1", "work/b.py": b"2"},
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["details"]["reason"], "too_many_entries")

        with mock.patch.object(works, "MAX_WORK_FILE_BYTES", 2):
            status, _, body = self.sync(
                token, playground_id, commit_id, {"work/a.py": b"abc"}
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["details"]["reason"], "file_too_large")

        with mock.patch.object(works, "MAX_WORK_TOTAL_BYTES", 2):
            status, _, body = self.sync(
                token,
                playground_id,
                commit_id,
                {"work/a.py": b"ab", "work/b.py": b"cd"},
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["details"]["reason"], "work_too_large")

        self.assertIsNone(self.work_head(playground_id, student.id))

    def test_oversized_json_body_is_rejected_before_reading(self):
        _, _, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        # The declared length is over the ceiling while the sent body stays
        # small, so the server must answer 413 without consuming the stream.
        status, _, body = self.request(
            "POST",
            self.sync_path(playground_id),
            raw_body=b"{}",
            token=token,
            extra_headers={
                "Content-Length": str(works.MAX_SYNC_JSON_BODY_BYTES + 1),
            },
        )
        self.assertEqual((status, body["code"]), (413, "request_too_large"))

    def test_error_responses_never_echo_source_bytes(self):
        _, _, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        secret = b"TOP-SECRET-WORK-SOURCE-BYTES"
        encoded = base64.b64encode(secret).decode("ascii")

        # The data decodes successfully; the request fails on the path, so the
        # error is produced after the source bytes were already in memory.
        status, _, body = self.request(
            "POST",
            self.sync_path(playground_id),
            {
                "playgroundCommitId": commit_id,
                "baseCommitId": None,
                "files": [{"path": "course/leak.py", "data": encoded}],
            },
            token=token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["details"]["reason"], "path_not_allowed")
        rendered = json.dumps(body)
        self.assertNotIn(encoded, rendered)
        self.assertNotIn(secret.decode("ascii"), rendered)

    # ----- authorization and conflict privacy -----------------------------

    def test_authentication_and_student_ownership(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)
        teacher = self.seed_user("teacher")
        teacher_token = self.login_token(teacher.username)
        student = self.seed_user("student")
        student_token = self.login_token(student.username)

        public_id = self.unique("pg")
        self.create_playground(admin_token, public_id)
        self.set_policy(admin_token, public_id, "public")
        public_commit = self.publish(admin_token, public_id)

        private_id = self.unique("pg")
        self.create_playground(admin_token, private_id)
        private_commit = self.publish(admin_token, private_id)

        public_body = {
            "playgroundCommitId": public_commit,
            "baseCommitId": None,
            "files": self.files({"work/a.py": b"x"}),
        }
        private_body = {
            "playgroundCommitId": private_commit,
            "baseCommitId": None,
            "files": self.files({"work/a.py": b"x"}),
        }

        for token in (None, "bogus"):
            status, _, response = self.request(
                "POST", self.sync_path(public_id), public_body, token=token
            )
            self.assertEqual(
                (status, response["code"]), (401, "invalid_session")
            )

        # Only Students own Work; Admin and Teacher are forbidden.
        for token in (admin_token, teacher_token):
            status, _, response = self.request(
                "POST", self.sync_path(public_id), public_body, token=token
            )
            self.assertEqual((status, response["code"]), (403, "forbidden"))
            self.assertNotIn("currentHeadCommitId", response)

        # A Student who cannot read the pinned commit is denied without
        # learning whether a Work HEAD exists.
        status, _, response = self.request(
            "POST", self.sync_path(private_id), private_body,
            token=student_token,
        )
        self.assertEqual((status, response["code"]), (403, "forbidden"))
        self.assertNotIn("currentHeadCommitId", response)
        self.assertIsNone(self.work_head(private_id, student.id))

    def test_conflict_does_not_expose_another_students_head(self):
        _, _, _, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        student_a = self.seed_user("student")
        token_a = self.login_token(student_a.username)
        student_b = self.seed_user("student")
        token_b = self.login_token(student_b.username)

        status, _, body = self.sync(
            token_a, playground_id, commit_id, {"work/main.py": b"a"}
        )
        self.assertEqual(status, 201)
        head_a = body["workCommitId"]

        # B presents A's HEAD as its base while B has no Work ref yet, so the
        # conflict must report B's own absent HEAD, never A's commit.
        status, _, body = self.sync(
            token_b, playground_id, commit_id, {"work/main.py": b"b"},
            base=head_a,
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "work_head_changed")
        self.assertIsNone(body["currentHeadCommitId"])
        self.assertNotIn(head_a, json.dumps(body))

    def test_pinned_playground_cannot_be_changed(self):
        admin_token, student, token, playground_id, first_commit = (
            self.public_playground_as_admin()
        )
        status, _, body = self.sync(
            token, playground_id, first_commit, {"work/main.py": b"a"}
        )
        self.assertEqual(status, 201)
        work_head = body["workCommitId"]

        second_commit = self.publish(
            admin_token, playground_id, b"print(2)"
        )
        self.assertNotEqual(second_commit, first_commit)

        status, _, body = self.sync(
            token,
            playground_id,
            second_commit,
            {"work/main.py": b"b"},
            base=work_head,
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            body["details"]["reason"], "work_playground_mismatch"
        )
        # The Work ref did not move and no second commit was written.
        self.assertEqual(
            self.work_head(playground_id, student.id), work_head
        )

    # ----- routing ---------------------------------------------------------

    def test_route_methods_and_paths(self):
        playground_id = self.unique("pg")
        path = self.sync_path(playground_id)
        for method in ("GET", "PUT", "PATCH", "DELETE"):
            status, headers, body = self.request(method, path)
            self.assertEqual(status, 405, method)
            self.assertEqual(headers.get("allow"), "POST", method)
            self.assertEqual(body["code"], "method_not_allowed")

        for bad in (
            f"/api/v1/works/{playground_id}/sync/extra",
            f"/api/v1/works/{playground_id}/other",
            "/api/v1/works//sync",
            "/api/v1/works/sync",
        ):
            status, _, body = self.request("POST", bad)
            self.assertEqual(
                (status, body["code"]), (404, "not_found"), bad
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
