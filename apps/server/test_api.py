"""HTTP tests for the account API."""

import base64
import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

import accounts
import bundles
import generated
import repository
import server
import works
from comments import MAX_COMMENT_BODY_BYTES
from database import open_database
from server import (
    MAX_COMMENT_JSON_BODY_BYTES,
    MAX_JSON_BODY_BYTES,
    make_server,
)


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
            audit_output=io.StringIO(),
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
            audit_output=io.StringIO(),
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
        self.assertEqual(
            set(membership),
            {"studentId", "classId", "username", "joinedAt"},
        )
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


    def test_class_members_list_requires_owner_and_shows_username(self):
        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        other_teacher = self.seed_user("teacher")
        student = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        teacher_token = self.login_token(teacher.username)
        other_teacher_token = self.login_token(other_teacher.username)
        student_token = self.login_token(student.username)

        klass = self.create_class(admin_token, teacher.id, "Algebra")
        status, _, _ = self.request(
            "PUT",
            f"/api/v1/admin/classes/{klass['id']}/members/{student.id}",
            token=admin_token,
        )
        self.assertEqual(status, 200)

        # Admin and the owning Teacher see the member's account name.
        for token in (admin_token, teacher_token):
            status, _, body = self.request(
                "GET",
                f"/api/v1/classes/{klass['id']}/members",
                token=token,
            )
            self.assertEqual(status, 200)
            self.assertEqual(len(body["members"]), 1)
            member = body["members"][0]
            self.assertEqual(
                set(member),
                {"studentId", "classId", "username", "joinedAt"},
            )
            self.assertEqual(member["studentId"], student.id)
            self.assertEqual(member["classId"], klass["id"])
            self.assertEqual(member["username"], student.username)
            self.assertTrue(member["joinedAt"])

        # A different Teacher and a Student are denied.
        for token in (other_teacher_token, student_token):
            status, _, body = self.request(
                "GET",
                f"/api/v1/classes/{klass['id']}/members",
                token=token,
            )
            self.assertEqual((status, body["code"]), (403, "forbidden"))

        # An unknown Class is hidden, and an anonymous read is a 401.
        status, _, body = self.request(
            "GET", "/api/v1/classes/nope/members", token=admin_token
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))
        status, _, body = self.request(
            "GET", f"/api/v1/classes/{klass['id']}/members"
        )
        self.assertEqual((status, body["code"]), (401, "invalid_session"))


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
            audit_output=io.StringIO(),
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
                self.build_zip(
                    (
                        ("picopg.json", self.MANIFEST),
                        ("course/a", b"file"),
                        ("course/a/b.py", b"nested"),
                    )
                ),
                bundles.REASON_PATH_FILE_DIRECTORY,
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

    def test_playground_creation_rejects_invalid_ids(self):
        admin = self.seed_user("admin")
        token = self.login_token(admin.username)
        invalid_ids = (
            "../escape",
            "bad/id",
            "bad\\id",
            "bad id",
            "bad%2fid",
            ".leading",
            "trailing.",
            "double..dot",
            "name.lock",
            "NAME.LOCK",
            "caf\u00e9",
            "has\x01control",
            "a" * 129,
            "",
        )
        for playground_id in invalid_ids:
            with self.subTest(playground_id=playground_id):
                status, _, body = self.request(
                    "POST",
                    "/api/v1/playgrounds",
                    {"id": playground_id},
                    token=token,
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["code"], "invalid_request")

        connection = open_database(self.database_path)
        try:
            stored = {
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM playgrounds"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertEqual(stored.intersection(invalid_ids), set())

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
            audit_output=io.StringIO(),
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
            audit_output=io.StringIO(),
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
            audit_output=io.StringIO(),
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
        self.assertEqual(
            [
                item
                for item in body["catalog"]
                if item["playgroundId"] == playground_id
            ],
            [],
        )

        status, body = self.request(
            "GET",
            f"/api/v1/playgrounds/{playground_id}"
            f"/commits/{commit_id}/manifest",
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

    def test_publish_refreshes_generation_and_rebuild_failure_is_nonfatal(
        self,
    ):
        token = self.seed_admin_token()
        playground_id = f"public-{uuid.uuid4().hex[:8]}"
        self.assertEqual(
            self.request(
                "POST", "/api/v1/playgrounds", {"id": playground_id}, token
            )[0],
            201,
        )
        self.assertEqual(
            self.request(
                "PATCH",
                f"/api/v1/playgrounds/{playground_id}",
                {"policy": "public"},
                token,
            )[0],
            200,
        )
        status, body = self.request(
            "POST",
            f"/api/v1/playgrounds/{playground_id}/commits",
            token=token,
            raw_body=self.build_zip(
                (("picopg.json", self.MANIFEST), ("course/a.py", b"print(1)"))
            ),
            content_type="application/zip",
            extra_headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        self.assertEqual(status, 201)
        commit_id = body["commitId"]

        # The publish itself rebuilt the current generation from authority.
        generation = generated.resolve_current_generation(self.generated_path)
        bootstrap = json.loads(generation.bootstrap)
        self.assertEqual(
            bootstrap["catalog"],
            [{"playgroundId": playground_id, "commitId": commit_id}],
        )

        # A failed post-authority rebuild preserves authority and still
        # returns the mutation's success response.
        second_id = f"public-{uuid.uuid4().hex[:8]}"
        self.assertEqual(
            self.request(
                "POST", "/api/v1/playgrounds", {"id": second_id}, token
            )[0],
            201,
        )
        self.assertEqual(
            self.request(
                "PATCH",
                f"/api/v1/playgrounds/{second_id}",
                {"policy": "public"},
                token,
            )[0],
            200,
        )
        with mock.patch.object(
            generated,
            "rebuild",
            side_effect=generated.GeneratedCacheError("injected"),
        ):
            status, body = self.request(
                "POST",
                f"/api/v1/playgrounds/{second_id}/commits",
                token=token,
                raw_body=self.build_zip(
                    (
                        ("picopg.json", self.MANIFEST),
                        ("course/b.py", b"print(2)"),
                    )
                ),
                content_type="application/zip",
                extra_headers={"Idempotency-Key": uuid.uuid4().hex},
            )
        self.assertEqual(status, 201)
        second_commit = body["commitId"]

        # The old generation is still current; authority is intact.
        preserved = generated.resolve_current_generation(self.generated_path)
        self.assertEqual(preserved.generation_id, generation.generation_id)
        status, body = self.request(
            "GET", f"/api/v1/playgrounds/{second_id}"
            f"/commits/{second_commit}/manifest"
        )
        self.assertEqual(status, 200)

        # A later rebuild converges to current authority.
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
        refreshed = generated.resolve_current_generation(self.generated_path)
        catalog = json.loads(refreshed.bootstrap)["catalog"]
        self.assertEqual(
            {item["playgroundId"] for item in catalog},
            {playground_id, second_id},
        )


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
            audit_output=io.StringIO(),
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

    # ----- Work content reads --------------------------------------------

    def _raw_work_file(self, path, method="GET", token=None):
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

    @staticmethod
    def _work_file_path(playground_id, commit_id, encoded_path):
        return (
            f"/api/v1/works/{playground_id}/commits/{commit_id}"
            f"/files/{encoded_path}"
        )

    def test_work_content_head_manifest_and_file_flow(self):
        _, _, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        status, _, body = self.request(
            "GET", f"/api/v1/works/{playground_id}/head", token=token
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

        status, _, body = self.sync(
            token,
            playground_id,
            commit_id,
            {
                "work/main.py": b"print('work')\n",
                "work/data.bin": b"\x00\xff",
                "work/empty.bin": b"",
            },
        )
        self.assertEqual(status, 201)
        work_commit_id = body["workCommitId"]

        status, _, body = self.request(
            "GET", f"/api/v1/works/{playground_id}/head", token=token
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            body["work"],
            {
                "playgroundId": playground_id,
                "workCommitId": work_commit_id,
                "playgroundCommitId": commit_id,
            },
        )

        manifest_path = (
            f"/api/v1/works/{playground_id}/commits/{work_commit_id}"
            "/manifest"
        )
        status, _, body = self.request("GET", manifest_path, token=token)
        self.assertEqual(status, 200)
        manifest = body["manifest"]
        self.assertEqual(manifest["workCommitId"], work_commit_id)
        self.assertEqual(manifest["playgroundCommitId"], commit_id)
        self.assertEqual(
            [item["path"] for item in manifest["files"]],
            ["data.bin", "empty.bin", "main.py"],
        )

        file_path = self._work_file_path(
            playground_id, work_commit_id, "data.bin"
        )
        status, headers, body = self._raw_work_file(file_path, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"\x00\xff")
        self.assertEqual(headers["content-type"], "application/octet-stream")
        self.assertEqual(headers["content-length"], "2")

        status, headers, body = self._raw_work_file(
            self._work_file_path(playground_id, work_commit_id, "empty.bin"),
            token=token,
        )
        self.assertEqual((status, body), (200, b""))

        # HEAD has the GET status and headers with an empty body.
        status, headers, body = self._raw_work_file(
            file_path, method="HEAD", token=token
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers["content-length"], "2")

    def test_work_content_history_pagination_and_cursor(self):
        _, _, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        _, _, first = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"first"}
        )
        _, _, second = self.sync(
            token,
            playground_id,
            commit_id,
            {"work/main.py": b"second"},
            base=first["workCommitId"],
        )
        first_id = first["workCommitId"]
        second_id = second["workCommitId"]

        status, _, body = self.request(
            "GET", f"/api/v1/works/{playground_id}/history", token=token
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["workCommitId"] for item in body["commits"]],
            [second_id, first_id],
        )
        self.assertEqual(
            body["commits"][0]["parentWorkCommitId"], first_id
        )
        self.assertIsNone(body["commits"][1]["parentWorkCommitId"])
        self.assertIsNone(body["nextCursor"])

        status, _, body = self.request(
            "GET",
            f"/api/v1/works/{playground_id}/history?limit=1",
            token=token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["workCommitId"] for item in body["commits"]], [second_id]
        )
        self.assertEqual(body["nextCursor"], second_id)

        status, _, body = self.request(
            "GET",
            f"/api/v1/works/{playground_id}/history?limit=1&cursor={second_id}",
            token=token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["workCommitId"] for item in body["commits"]], [first_id]
        )
        self.assertIsNone(body["nextCursor"])

    def test_work_content_history_query_validation(self):
        _, _, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        self.sync(token, playground_id, commit_id, {"work/main.py": b"x"})

        for query in (
            "?limit=0",
            "?limit=101",
            "?limit=",
            "?limit=1&limit=2",
            "?limit=1.0",
            "?limit=-1",
            "?unknown=1",
            "?cursor=",
            "?cursor=" + "0" * 40,
            "?cursor=not-a-commit",
        ):
            with self.subTest(query=query):
                status, _, body = self.request(
                    "GET",
                    f"/api/v1/works/{playground_id}/history{query}",
                    token=token,
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["code"], "invalid_request")

        # A cursor from another Student's Work is not in this history.
        other = self.seed_user("student")
        other_token = self.login_token(other.username)
        _, _, other_body = self.sync(
            other_token, playground_id, commit_id, {"work/main.py": b"other"}
        )
        other_cursor = other_body["workCommitId"]
        status, _, body = self.request(
            "GET",
            f"/api/v1/works/{playground_id}/history?cursor={other_cursor}",
            token=token,
        )
        self.assertEqual(status, 400)

    def test_work_content_hidden_denials(self):
        _, _, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        _, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"x"}
        )
        work_commit_id = body["workCommitId"]
        manifest_path = (
            f"/api/v1/works/{playground_id}/commits/{work_commit_id}"
            "/manifest"
        )

        # Anonymous and an invalid session stay 401.
        for bad_token in (None, "not-a-session"):
            status, _, _ = self.request(
                "GET", f"/api/v1/works/{playground_id}/head", token=bad_token
            )
            self.assertEqual(status, 401)
            status, _, _ = self.request(
                "GET", manifest_path, token=bad_token
            )
            self.assertEqual(status, 401)

        # Another Student sees an empty own history, but the same hidden 404
        # as an unknown Work resource for the head, manifest, and file.
        other = self.seed_user("student")
        other_token = self.login_token(other.username)
        for path in (
            f"/api/v1/works/{playground_id}/head",
            manifest_path,
            self._work_file_path(playground_id, work_commit_id, "main.py"),
        ):
            status, _, response = self.request("GET", path, token=other_token)
            self.assertEqual(status, 404, path)
            self.assertEqual(response["code"], "not_found")
        status, _, body = self.request(
            "GET",
            f"/api/v1/works/{playground_id}/history",
            token=other_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["commits"], [])

        # A Teacher and an Admin receive the hidden 404 on every route.
        for role in ("teacher", "admin"):
            user = self.seed_user(role)
            user_token = self.login_token(user.username)
            for path in (
                f"/api/v1/works/{playground_id}/head",
                f"/api/v1/works/{playground_id}/history",
                manifest_path,
                self._work_file_path(
                    playground_id, work_commit_id, "main.py"
                ),
            ):
                status, _, response = self.request(
                    "GET", path, token=user_token
                )
                self.assertEqual(status, 404, (role, path))
                self.assertEqual(response["code"], "not_found")

    def test_work_content_file_path_rejections(self):
        _, _, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        _, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"x"}
        )
        work_commit_id = body["workCommitId"]
        for encoded in (
            "..%2fsecret.py",
            "..%5csecret.py",
            "missing.py",
            "course%2fmodule%2fapp.py",
            ".picopg%2fwork.json",
            "work%2fmain.py",
        ):
            with self.subTest(encoded=encoded):
                status, _, response = self.request(
                    "GET",
                    self._work_file_path(
                        playground_id, work_commit_id, encoded
                    ),
                    token=token,
                )
                self.assertEqual(status, 404)
                self.assertEqual(response["code"], "not_found")

        status, _, response = self.request(
            "GET",
            f"/api/v1/works/{playground_id}/commits/not-a-commit/manifest",
            token=token,
        )
        self.assertEqual((status, response["code"]), (404, "not_found"))

    def test_work_content_reads_do_not_mutate(self):
        _, student, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        _, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"x"}
        )
        work_commit_id = body["workCommitId"]
        ref = self.server.repository.work_ref(playground_id, student.id)

        def git(*arguments):
            return subprocess.run(
                ["git", "-C", str(self.repository_path), *arguments],
                capture_output=True,
                check=True,
            ).stdout

        before_ref = git("rev-parse", ref)
        connection = open_database(self.database_path)
        try:
            before = connection.execute(
                "SELECT COUNT(*) AS n FROM submissions"
            ).fetchone()["n"]
        finally:
            connection.close()

        for path in (
            f"/api/v1/works/{playground_id}/head",
            f"/api/v1/works/{playground_id}/history",
            f"/api/v1/works/{playground_id}/commits/{work_commit_id}/manifest",
        ):
            _, _, _ = self.request("GET", path, token=token)
        self._raw_work_file(
            self._work_file_path(playground_id, work_commit_id, "main.py"),
            token=token,
        )

        self.assertEqual(git("rev-parse", ref), before_ref)
        connection = open_database(self.database_path)
        try:
            after = connection.execute(
                "SELECT COUNT(*) AS n FROM submissions"
            ).fetchone()["n"]
        finally:
            connection.close()
        self.assertEqual(after, before)


class WorkRestoreApiTest(unittest.TestCase):
    """HTTP contract for the append-only Work restoration endpoint."""

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
            audit_output=io.StringIO(),
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

    # ----- generic HTTP helpers -----------------------------------------

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

    # ----- Work helpers --------------------------------------------------

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
            f"/api/v1/works/{playground_id}/sync",
            {
                "playgroundCommitId": playground_commit_id,
                "baseCommitId": base,
                "files": self.files(mapping),
            },
            token=token,
        )

    def restore(self, token, playground_id, base, source):
        return self.request(
            "POST",
            f"/api/v1/works/{playground_id}/restore",
            {"baseCommitId": base, "sourceCommitId": source},
            token=token,
        )

    def work_head(self, playground_id, student_id):
        return repository.resolve_work_head(
            self.server.repository, playground_id, student_id
        )

    def work_ref(self, playground_id, student_id):
        return self.server.repository.work_ref(playground_id, student_id)

    def parents(self, commit_id):
        return [
            value.decode("ascii")
            for value in self._git(
                "rev-list", "--parents", "-n", "1", commit_id
            ).split()[1:]
        ]

    def tree_id(self, commit_id):
        return (
            self._git("rev-parse", f"{commit_id}^{{tree}}")
            .strip()
            .decode("ascii")
        )

    # ----- direct Git helpers (for crafted histories) --------------------

    def _git(self, *arguments, input=None):
        completed = subprocess.run(
            ["git", "-C", str(self.repository_path), *arguments],
            input=input,
            capture_output=True,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8", "replace")
        )
        return completed.stdout

    def _write_tree_node(self, node):
        lines = []
        for name in sorted(node):
            value = node[name]
            if isinstance(value, dict):
                object_id = self._write_tree_node(value)
                lines.append(f"040000 tree {object_id}\t{name}")
            else:
                blob = (
                    self._git("hash-object", "-w", "--stdin", input=value)
                    .strip()
                    .decode("ascii")
                )
                lines.append(f"100644 blob {blob}\t{name}")
        return (
            self._git("mktree", input=("\n".join(lines) + "\n").encode())
            .strip()
            .decode("ascii")
        )

    def craft_commit(self, files, parent=None):
        root = {}
        for path, data in files.items():
            node = root
            for part in path.split("/")[:-1]:
                node = node.setdefault(part, {})
            node[path.split("/")[-1]] = data
        tree = self._write_tree_node(root)
        arguments = ["commit-tree", tree]
        if parent is not None:
            arguments += ["-p", parent]
        return (
            self._git(
                "-c", "user.name=Test Author",
                "-c", "user.email=author@example.test",
                *arguments,
                input=b"crafted\n",
            )
            .strip()
            .decode("ascii")
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

    # ----- success -------------------------------------------------------

    def test_restore_appends_and_returns_the_new_commit(self):
        _, student, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        status, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"a"}
        )
        self.assertEqual(status, 201)
        a = body["workCommitId"]
        status, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"b"},
            base=a,
        )
        self.assertEqual(status, 200)
        b = body["workCommitId"]
        status, _, body = self.sync(
            token,
            playground_id,
            commit_id,
            {"work/main.py": b"c", "work/extra.py": b"e"},
            base=b,
        )
        self.assertEqual(status, 200)
        c = body["workCommitId"]

        status, _, body = self.restore(token, playground_id, c, a)
        self.assertEqual(status, 201)
        new = body["workCommitId"]
        self.assertNotEqual(new, c)
        self.assertEqual(body["playgroundCommitId"], commit_id)
        self.assertEqual(self.work_head(playground_id, student.id), new)
        # New commit D has C as its sole parent and A's exact tree.
        self.assertEqual(self.parents(new), [c])
        self.assertEqual(self.tree_id(new), self.tree_id(a))
        tree = {
            entry.path
            for entry in repository.list_commit_tree(
                self.server.repository, new
            )
        }
        self.assertNotIn("work/extra.py", tree)

    # ----- conflict and privacy -----------------------------------------

    def test_stale_base_conflicts_with_canonical_head(self):
        _, _, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        status, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"a"}
        )
        a = body["workCommitId"]
        status, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"b"}, base=a
        )
        b = body["workCommitId"]

        status, _, body = self.restore(token, playground_id, a, a)
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "work_head_changed")
        self.assertEqual(body["currentHeadCommitId"], b)

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
        head_a = body["workCommitId"]

        # B presents A's HEAD as its base while B has no Work ref, so the
        # conflict must report B's own absent HEAD, never A's commit.
        status, _, body = self.restore(
            token_b, playground_id, head_a, head_a
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "work_head_changed")
        self.assertIsNone(body["currentHeadCommitId"])
        self.assertNotIn(head_a, json.dumps(body))

    def test_restore_requires_an_existing_work(self):
        _, _, token, playground_id, _ = self.public_playground_as_admin()
        status, _, body = self.restore(
            token, playground_id, "a" * 40, "a" * 40
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "work_head_changed")

    # ----- rejection mapping --------------------------------------------

    def test_authentication_and_student_ownership(self):
        admin_token, student, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        status, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"a"}
        )
        head = body["workCommitId"]

        for bad_token in (None, "bogus"):
            status, _, body = self.restore(
                bad_token, playground_id, head, head
            )
            self.assertEqual(
                (status, body["code"]), (401, "invalid_session")
            )

        teacher = self.seed_user("teacher")
        teacher_token = self.login_token(teacher.username)
        for actor_token in (admin_token, teacher_token):
            status, _, body = self.restore(
                actor_token, playground_id, head, head
            )
            self.assertEqual((status, body["code"]), (403, "forbidden"))
            self.assertNotIn("currentHeadCommitId", body)
        self.assertEqual(self.work_head(playground_id, student.id), head)

    def test_foreign_or_nonancestor_source_is_denied(self):
        _, student, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        status, _, body = self.sync(
            token, playground_id, commit_id, {"work/main.py": b"mine"}
        )
        head = body["workCommitId"]

        other = self.seed_user("student")
        other_token = self.login_token(other.username)
        status, _, body = self.sync(
            other_token, playground_id, commit_id, {"work/main.py": b"theirs"}
        )
        self.assertEqual(status, 201)
        other_head = body["workCommitId"]

        # A commit from another Student's Work is not in this history.
        status, _, body = self.restore(token, playground_id, head, other_head)
        self.assertEqual((status, body["code"]), (403, "forbidden"))
        # A valid commit from another Playground is not in this history.
        other_playground = self.unique("pg")
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)
        self.create_playground(admin_token, other_playground)
        self.set_policy(admin_token, other_playground, "public")
        other_commit = self.publish(admin_token, other_playground)
        status, _, body = self.sync(
            token, other_playground, other_commit, {"work/main.py": b"x"}
        )
        elsewhere = body["workCommitId"]
        status, _, body = self.restore(
            token, playground_id, head, elsewhere
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))
        self.assertEqual(self.work_head(playground_id, student.id), head)

    def test_restore_rejects_invalid_payload(self):
        _, _, token, playground_id, _ = self.public_playground_as_admin()
        path = f"/api/v1/works/{playground_id}/restore"
        for payload in (
            {},
            {"baseCommitId": "a" * 40},
            {"sourceCommitId": "a" * 40},
            {"baseCommitId": None, "sourceCommitId": "a" * 40},
            {"baseCommitId": "a" * 40, "sourceCommitId": ""},
        ):
            with self.subTest(payload=payload):
                status, _, body = self.request(
                    "POST", path, payload, token=token
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["code"], "invalid_request")
                self.assertEqual(
                    body["details"]["reason"], "invalid_payload"
                )

    def test_corrupt_manifest_source_is_rejected(self):
        _, student, token, playground_id, commit_id = (
            self.public_playground_as_admin()
        )
        commit = self.craft_commit(
            {
                "work/main.py": b"x",
                ".picopg/work.json": b"{",
            }
        )
        self._git(
            "update-ref",
            self.work_ref(playground_id, student.id),
            commit,
        )
        status, _, body = self.restore(token, playground_id, commit, commit)
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_request")
        self.assertEqual(
            body["details"]["reason"], "work_manifest_invalid"
        )
        self.assertEqual(self.work_head(playground_id, student.id), commit)

    def test_playground_pin_mismatch_is_rejected(self):
        admin_token, student, token, playground_id, first_commit = (
            self.public_playground_as_admin()
        )
        second_commit = self.publish(
            admin_token, playground_id, b"print(2)"
        )
        pin = lambda value: (
            '{"playgroundCommitId":"' + value + '"}'
        ).encode("utf-8")
        x = self.craft_commit(
            {
                "work/main.py": b"x",
                ".picopg/work.json": pin(second_commit),
            }
        )
        y = self.craft_commit(
            {
                "work/main.py": b"y",
                ".picopg/work.json": pin(first_commit),
            },
            parent=x,
        )
        self._git(
            "update-ref", self.work_ref(playground_id, student.id), y
        )
        status, _, body = self.restore(token, playground_id, y, x)
        self.assertEqual(status, 400)
        self.assertEqual(
            body["details"]["reason"], "work_playground_mismatch"
        )
        self.assertEqual(self.work_head(playground_id, student.id), y)

    # ----- routing -------------------------------------------------------

    def test_restore_route_methods_and_paths(self):
        playground_id = self.unique("pg")
        path = f"/api/v1/works/{playground_id}/restore"
        for method in ("GET", "PUT", "PATCH", "DELETE"):
            status, headers, body = self.request(method, path)
            self.assertEqual(status, 405, method)
            self.assertEqual(headers.get("allow"), "POST", method)
            self.assertEqual(body["code"], "method_not_allowed")

        for bad in (
            f"/api/v1/works/{playground_id}/restore/extra",
            f"/api/v1/works/{playground_id}/other",
            "/api/v1/works//restore",
            "/api/v1/works/restore",
        ):
            status, _, body = self.request("POST", bad)
            self.assertEqual(
                (status, body["code"]), (404, "not_found"), bad
            )


class SubmissionApiTest(unittest.TestCase):
    """HTTP contract for the immutable Submission API."""

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
            audit_output=io.StringIO(),
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

    def count_submissions_for(self, student_id):
        connection = open_database(self.database_path)
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM submissions"
                " WHERE student_id = ?",
                (student_id,),
            ).fetchone()
            return row["total"]
        finally:
            connection.close()

    def create_class(self, admin_token, teacher_id, name="Class"):
        status, _, body = self.request(
            "POST",
            "/api/v1/admin/classes",
            {"name": name, "teacherId": teacher_id},
            token=admin_token,
        )
        self.assertEqual(status, 201)
        return body["class"]

    def assign_student(self, admin_token, class_id, student_id):
        status, _, _ = self.request(
            "PUT",
            f"/api/v1/admin/classes/{class_id}/members/{student_id}",
            token=admin_token,
        )
        self.assertEqual(status, 200)

    def create_playground(self, token, playground_id):
        status, _, body = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": playground_id},
            token=token,
        )
        self.assertEqual(status, 201)
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

    def sync(
        self, token, playground_id, playground_commit_id, mapping, base=None
    ):
        files = [
            {"path": path, "data": base64.b64encode(data).decode("ascii")}
            for path, data in mapping.items()
        ]
        status, _, body = self.request(
            "POST",
            f"/api/v1/works/{playground_id}/sync",
            {
                "playgroundCommitId": playground_commit_id,
                "baseCommitId": base,
                "files": files,
            },
            token=token,
        )
        self.assertIn(status, (200, 201), body)
        return body["workCommitId"]

    def submit(self, token, release_id, work_commit_id, key):
        headers = {} if key is None else {"Idempotency-Key": key}
        return self.request(
            "POST",
            f"/api/v1/releases/{release_id}/submissions",
            {"workCommitId": work_commit_id},
            token=token,
            extra_headers=headers,
        )

    def list_submissions(self, token, release_id):
        return self.request(
            "GET",
            f"/api/v1/releases/{release_id}/submissions",
            token=token,
        )

    def get_submission(self, token, submission_id):
        return self.request(
            "GET",
            f"/api/v1/submissions/{submission_id}",
            token=token,
        )

    def course(self):
        """Return an Admin/Teacher/Student course with open Release and Work."""

        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        student = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        teacher_token = self.login_token(teacher.username)
        student_token = self.login_token(student.username)
        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        commit_id = self.publish(admin_token, playground_id)
        klass = self.create_class(admin_token, teacher.id)
        self.assign_student(admin_token, klass["id"], student.id)
        release = self.post_release(
            teacher_token,
            klass["id"],
            self.unique("rel"),
            self.release_payload(playground_id, commit_id),
        )[2]["release"]
        work_commit_id = self.sync(
            student_token,
            playground_id,
            commit_id,
            {"work/main.py": b"print('work')"},
        )
        return {
            "admin": admin,
            "teacher": teacher,
            "student": student,
            "admin_token": admin_token,
            "teacher_token": teacher_token,
            "student_token": student_token,
            "class": klass,
            "playground_id": playground_id,
            "commit_id": commit_id,
            "release": release,
            "work_commit_id": work_commit_id,
        }

    def test_submit_success_shape_and_exact_retry(self):
        ctx = self.course()
        key = self.unique("submit")

        status, _, body = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            ctx["work_commit_id"],
            key,
        )
        self.assertEqual(status, 201)
        record = body["submission"]
        self.assertEqual(
            set(record),
            {
                "id",
                "releaseId",
                "studentId",
                "username",
                "workCommitId",
                "createdAt",
            },
        )
        self.assertEqual(record["releaseId"], ctx["release"]["id"])
        self.assertEqual(record["studentId"], ctx["student"].id)
        self.assertEqual(record["workCommitId"], ctx["work_commit_id"])
        self.assertTrue(record["createdAt"])
        # No source or mutable status is embedded in the metadata response.
        self.assertNotIn("files", record)
        self.assertNotIn("content", record)
        self.assertNotIn("status", record)

        retry_status, _, retry = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            ctx["work_commit_id"],
            key,
        )
        self.assertEqual(retry_status, 201)
        self.assertEqual(retry, body)

        listed_status, _, listed = self.list_submissions(
            ctx["student_token"], ctx["release"]["id"]
        )
        self.assertEqual(listed_status, 200)
        self.assertEqual(listed["submissions"], [record])
        self.assertEqual(self.count_submissions_for(ctx["student"].id), 1)

    def test_changed_body_under_same_key_conflicts(self):
        ctx = self.course()
        key = self.unique("submit")
        status, _, first = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            ctx["work_commit_id"],
            key,
        )
        self.assertEqual(status, 201)

        second_work = self.sync(
            ctx["student_token"],
            ctx["playground_id"],
            ctx["commit_id"],
            {"work/main.py": b"print('second')"},
            base=ctx["work_commit_id"],
        )
        self.assertNotEqual(second_work, ctx["work_commit_id"])

        status, _, body = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            second_work,
            key,
        )
        self.assertEqual(
            (status, body["code"]), (409, "idempotency_conflict")
        )

        _, _, listed = self.list_submissions(
            ctx["student_token"], ctx["release"]["id"]
        )
        self.assertEqual(len(listed["submissions"]), 1)
        self.assertEqual(
            listed["submissions"][0]["workCommitId"],
            ctx["work_commit_id"],
        )
        self.assertEqual(self.count_submissions_for(ctx["student"].id), 1)

    def test_time_window_failures(self):
        ctx = self.course()

        future = self.post_release(
            ctx["teacher_token"],
            ctx["class"]["id"],
            self.unique("rel"),
            self.release_payload(
                ctx["playground_id"],
                ctx["commit_id"],
                open_at="2999-01-01T00:00:00Z",
            ),
        )[2]["release"]
        status, _, body = self.submit(
            ctx["student_token"],
            future["id"],
            ctx["work_commit_id"],
            self.unique("submit"),
        )
        self.assertEqual((status, body["code"]), (409, "submission_closed"))

        due = self.post_release(
            ctx["teacher_token"],
            ctx["class"]["id"],
            self.unique("rel"),
            self.release_payload(
                ctx["playground_id"],
                ctx["commit_id"],
                due_at="2020-01-08T00:00:00Z",
                requires_submission=True,
            ),
        )[2]["release"]
        status, _, body = self.submit(
            ctx["student_token"],
            due["id"],
            ctx["work_commit_id"],
            self.unique("submit"),
        )
        self.assertEqual((status, body["code"]), (409, "submission_closed"))

        self.assertEqual(self.count_submissions_for(ctx["student"].id), 0)

    def test_membership_and_unknown_release_failures(self):
        ctx = self.course()
        outsider = self.seed_user("student")
        outsider_token = self.login_token(outsider.username)

        status, _, body = self.submit(
            outsider_token,
            ctx["release"]["id"],
            ctx["work_commit_id"],
            self.unique("submit"),
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))

        status, _, body = self.submit(
            ctx["student_token"],
            "missing-release",
            ctx["work_commit_id"],
            self.unique("submit"),
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))
        self.assertEqual(self.count_submissions_for(ctx["student"].id), 0)

    def test_version_mismatch_failures(self):
        ctx = self.course()

        # A well-formed commit that is on no Work history.
        status, _, body = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            "a" * 40,
            self.unique("submit"),
        )
        self.assertEqual(
            (status, body["code"]), (409, "playground_version_mismatch")
        )

        # A real Work commit that pins a different Playground commit than the
        # Release fixes.
        second_commit = self.publish(
            ctx["admin_token"], ctx["playground_id"], b"print(2)"
        )
        self.post_release(
            ctx["teacher_token"],
            ctx["class"]["id"],
            self.unique("rel"),
            self.release_payload(ctx["playground_id"], second_commit),
        )
        second_student = self.seed_user("student")
        self.assign_student(
            ctx["admin_token"], ctx["class"]["id"], second_student.id
        )
        second_token = self.login_token(second_student.username)
        second_work = self.sync(
            second_token,
            ctx["playground_id"],
            second_commit,
            {"work/main.py": b"print('other')"},
        )
        status, _, body = self.submit(
            second_token,
            ctx["release"]["id"],
            second_work,
            self.unique("submit"),
        )
        self.assertEqual(
            (status, body["code"]), (409, "playground_version_mismatch")
        )

        # A malformed commit is an invalid request, not a version conflict.
        for malformed in ("HEAD", "abc", "A" * 40):
            status, _, body = self.submit(
                ctx["student_token"],
                ctx["release"]["id"],
                malformed,
                self.unique("submit"),
            )
            self.assertEqual(
                (status, body["code"]), (400, "invalid_request"), malformed
            )

        self.assertEqual(self.count_submissions_for(ctx["student"].id), 0)
        self.assertEqual(self.count_submissions_for(second_student.id), 0)

    def test_student_reads_only_own_submissions(self):
        ctx = self.course()
        other = self.seed_user("student")
        self.assign_student(ctx["admin_token"], ctx["class"]["id"], other.id)
        other_token = self.login_token(other.username)
        other_work = self.sync(
            other_token,
            ctx["playground_id"],
            ctx["commit_id"],
            {"work/main.py": b"print('other')"},
        )
        first = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            ctx["work_commit_id"],
            self.unique("submit"),
        )[2]["submission"]
        second = self.submit(
            other_token,
            ctx["release"]["id"],
            other_work,
            self.unique("submit"),
        )[2]["submission"]

        status, _, listed = self.list_submissions(
            ctx["student_token"], ctx["release"]["id"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in listed["submissions"]], [first["id"]]
        )

        status, _, body = self.get_submission(
            ctx["student_token"], first["id"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["submission"], first)

        status, _, body = self.get_submission(
            ctx["student_token"], second["id"]
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))
        status, _, body = self.get_submission(other_token, first["id"])
        self.assertEqual((status, body["code"]), (404, "not_found"))

        # An unrelated Student cannot see the Release or its Submissions.
        outsider = self.seed_user("student")
        outsider_token = self.login_token(outsider.username)
        status, _, body = self.list_submissions(
            outsider_token, ctx["release"]["id"]
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))
        status, _, body = self.get_submission(outsider_token, first["id"])
        self.assertEqual((status, body["code"]), (404, "not_found"))

    def test_teacher_reads_are_scoped_to_owned_classes(self):
        ctx = self.course()
        other_teacher = self.seed_user("teacher")
        other_class = self.create_class(
            ctx["admin_token"], other_teacher.id, "Other"
        )
        other_teacher_token = self.login_token(other_teacher.username)
        other_release = self.post_release(
            other_teacher_token,
            other_class["id"],
            self.unique("rel"),
            self.release_payload(ctx["playground_id"], ctx["commit_id"]),
        )[2]["release"]
        other_student = self.seed_user("student")
        self.assign_student(
            ctx["admin_token"], other_class["id"], other_student.id
        )
        other_student_token = self.login_token(other_student.username)
        other_work = self.sync(
            other_student_token,
            ctx["playground_id"],
            ctx["commit_id"],
            {"work/main.py": b"print('other')"},
        )
        first = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            ctx["work_commit_id"],
            self.unique("submit"),
        )[2]["submission"]
        second = self.submit(
            other_student_token,
            other_release["id"],
            other_work,
            self.unique("submit"),
        )[2]["submission"]

        status, _, listed = self.list_submissions(
            ctx["teacher_token"], ctx["release"]["id"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in listed["submissions"]], [first["id"]]
        )
        status, _, body = self.list_submissions(
            ctx["teacher_token"], other_release["id"]
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))
        status, _, body = self.get_submission(
            ctx["teacher_token"], second["id"]
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

        status, _, listed = self.list_submissions(
            other_teacher_token, other_release["id"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in listed["submissions"]], [second["id"]]
        )
        status, _, body = self.get_submission(
            other_teacher_token, first["id"]
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

    def test_admin_metadata_never_reveals_work_source(self):
        ctx = self.course()
        submission = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            ctx["work_commit_id"],
            self.unique("submit"),
        )[2]["submission"]

        status, _, body = self.get_submission(
            ctx["admin_token"], submission["id"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["submission"], submission)
        self.assertNotIn("files", body["submission"])
        self.assertNotIn("content", body["submission"])

        status, _, listed = self.list_submissions(
            ctx["admin_token"], ctx["release"]["id"]
        )
        self.assertEqual(status, 200)
        self.assertIn(
            submission["id"],
            [item["id"] for item in listed["submissions"]],
        )

        # Metadata access is not a content grant: the centralized content
        # authorization denies the Work commit on the Playground path.
        work_commit = ctx["work_commit_id"]
        for path in (
            f"/api/v1/playgrounds/{ctx['playground_id']}/commits"
            f"/{work_commit}/manifest",
            f"/api/v1/playgrounds/{ctx['playground_id']}/commits"
            f"/{work_commit}/files/work/main.py",
        ):
            status, _, body = self.request(
                "GET", path, token=ctx["admin_token"]
            )
            self.assertEqual(
                (status, body["code"]), (404, "not_found"), path
            )

    def test_create_request_validation(self):
        ctx = self.course()
        path = f"/api/v1/releases/{ctx['release']['id']}/submissions"
        valid = {"workCommitId": ctx["work_commit_id"]}

        # A missing or blank idempotency key is rejected before the body.
        for headers in ({}, {"Idempotency-Key": "  "}):
            status, _, body = self.request(
                "POST",
                path,
                valid,
                token=ctx["student_token"],
                extra_headers=headers,
            )
            self.assertEqual((status, body["code"]), (400, "invalid_request"))

        status, _, body = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            ctx["work_commit_id"],
            "k" * 256,
        )
        self.assertEqual((status, body["code"]), (400, "invalid_request"))

        for invalid in (
            {},
            {"workCommitId": ""},
            {"workCommitId": "abc"},
            {"workCommitId": 7},
            {"workCommitId": None},
        ):
            status, _, body = self.request(
                "POST",
                path,
                invalid,
                token=ctx["student_token"],
                extra_headers={"Idempotency-Key": self.unique("submit")},
            )
            self.assertEqual(
                (status, body["code"]), (400, "invalid_request"), invalid
            )

        self.assertEqual(self.count_submissions_for(ctx["student"].id), 0)

    def test_unauthenticated_calls_are_rejected_first(self):
        ctx = self.course()
        calls = (
            self.submit(
                None,
                ctx["release"]["id"],
                ctx["work_commit_id"],
                "key",
            ),
            self.submit(
                "bogus",
                ctx["release"]["id"],
                ctx["work_commit_id"],
                "key",
            ),
            self.list_submissions(None, ctx["release"]["id"]),
            self.get_submission(None, "some-submission"),
        )
        for status, _, body in calls:
            self.assertEqual((status, body["code"]), (401, "invalid_session"))

    def test_route_methods_and_paths(self):
        release_id = self.unique("rel")
        submission_id = self.unique("sub")
        method_not_allowed = (
            (
                "PUT",
                f"/api/v1/releases/{release_id}/submissions",
                "GET, HEAD, POST",
            ),
            (
                "PATCH",
                f"/api/v1/releases/{release_id}/submissions",
                "GET, HEAD, POST",
            ),
            (
                "DELETE",
                f"/api/v1/releases/{release_id}/submissions",
                "GET, HEAD, POST",
            ),
            (
                "POST",
                "/api/v1/submissions",
                "GET, HEAD",
            ),
            (
                "POST",
                f"/api/v1/submissions/{submission_id}",
                "GET, HEAD",
            ),
            (
                "PUT",
                f"/api/v1/submissions/{submission_id}",
                "GET, HEAD",
            ),
            (
                "PATCH",
                f"/api/v1/submissions/{submission_id}",
                "GET, HEAD",
            ),
            (
                "DELETE",
                f"/api/v1/submissions/{submission_id}",
                "GET, HEAD",
            ),
        )
        for method, path, allow in method_not_allowed:
            status, headers, body = self.request(method, path)
            self.assertEqual(status, 405, (method, path))
            self.assertEqual(headers.get("allow"), allow, (method, path))
            self.assertEqual(body["code"], "method_not_allowed")

        not_found = (
            ("GET", "/api/v1/submissions/"),
            ("GET", "/api/v1/submissions/a/b"),
            ("GET", f"/api/v1/releases/{release_id}/submissions/extra"),
            ("POST", "/api/v1/releases//submissions"),
        )
        for method, path in not_found:
            status, _, body = self.request(method, path)
            self.assertEqual(
                (status, body["code"]), (404, "not_found"), (method, path)
            )

        # The own-Submissions collection exists, so an anonymous read is an
        # authentication failure rather than an unknown route.
        status, _, body = self.request("GET", "/api/v1/submissions")
        self.assertEqual((status, body["code"]), (401, "invalid_session"))


    def test_get_own_submissions_collection(self):
        ctx = self.course()
        status, _, body = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            ctx["work_commit_id"],
            self.unique("submit"),
        )
        self.assertEqual(status, 201)
        record = body["submission"]
        self.assertEqual(record["username"], ctx["student"].username)

        status, _, listed = self.request(
            "GET", "/api/v1/submissions", token=ctx["student_token"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(listed["submissions"], [record])

        # The owning Teacher sees the same record through the collection.
        status, _, listed = self.request(
            "GET", "/api/v1/submissions", token=ctx["teacher_token"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in listed["submissions"]], [record["id"]]
        )
        self.assertEqual(
            listed["submissions"][0]["username"],
            ctx["student"].username,
        )

        # A different Student sees only their own (empty) collection.
        other = self.seed_user("student")
        other_token = self.login_token(other.username)
        status, _, listed = self.request(
            "GET", "/api/v1/submissions", token=other_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(listed["submissions"], [])

        # HEAD returns the same status with an empty body.
        status, _, listed = self.request(
            "HEAD", "/api/v1/submissions", token=ctx["student_token"]
        )
        self.assertEqual(status, 200)
        self.assertIsNone(listed)

    # ----- Submission Work review ----------------------------------------

    def raw_submission_request(self, method, path, token=None):
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

    @staticmethod
    def submission_work_manifest_path(submission_id):
        return f"/api/v1/submissions/{submission_id}/work/manifest"

    @staticmethod
    def submission_work_file_path(submission_id, encoded_path):
        return (
            f"/api/v1/submissions/{submission_id}/work/files/{encoded_path}"
        )

    def test_submission_work_review_snapshot(self):
        ctx = self.course()
        submission = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            ctx["work_commit_id"],
            self.unique("submit"),
        )[2]["submission"]
        submission_id = submission["id"]
        manifest_path = self.submission_work_manifest_path(submission_id)
        file_path = self.submission_work_file_path(submission_id, "main.py")

        # The Student and the owning Teacher read the same immutable snapshot.
        for token in (ctx["student_token"], ctx["teacher_token"]):
            status, _, body = self.request("GET", manifest_path, token=token)
            self.assertEqual(status, 200)
            manifest = body["manifest"]
            self.assertEqual(manifest["submissionId"], submission_id)
            self.assertEqual(manifest["workCommitId"], ctx["work_commit_id"])
            self.assertEqual(manifest["playgroundId"], ctx["playground_id"])
            self.assertEqual(
                [item["path"] for item in manifest["files"]],
                ["main.py"],
            )
            status, _, data = self.raw_submission_request(
                "GET", file_path, token=token
            )
            self.assertEqual((status, data), (200, b"print('work')"))

        # A newer Student save never alters the submission snapshot.
        self.sync(
            ctx["student_token"],
            ctx["playground_id"],
            ctx["commit_id"],
            {"work/main.py": b"print('newer')"},
            base=ctx["work_commit_id"],
        )
        status, _, body = self.request(
            "GET", manifest_path, token=ctx["teacher_token"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            body["manifest"]["workCommitId"], ctx["work_commit_id"]
        )
        status, _, data = self.raw_submission_request(
            "GET", file_path, token=ctx["teacher_token"]
        )
        self.assertEqual((status, data), (200, b"print('work')"))

        # HEAD parity for the manifest and the file.
        status, headers, body = self.request(
            "HEAD", manifest_path, token=ctx["teacher_token"]
        )
        self.assertEqual(status, 200)
        self.assertIsNone(body)
        status, _, data = self.raw_submission_request(
            "HEAD", file_path, token=ctx["teacher_token"]
        )
        self.assertEqual((status, data), (200, b""))

    def test_submission_work_review_hidden_denials(self):
        ctx = self.course()
        submission_id = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            ctx["work_commit_id"],
            self.unique("submit"),
        )[2]["submission"]["id"]
        manifest_path = self.submission_work_manifest_path(submission_id)

        # Anonymous and invalid sessions stay 401.
        for bad_token in (None, "not-a-session"):
            status, _, _ = self.request(
                "GET", manifest_path, token=bad_token
            )
            self.assertEqual(status, 401)

        # Another Student, an unrelated Teacher, and an Admin all get the
        # same hidden 404 as a missing Submission.
        for role in ("student", "teacher", "admin"):
            user = self.seed_user(role)
            token = self.login_token(user.username)
            for path in (
                manifest_path,
                self.submission_work_file_path(submission_id, "main.py"),
                self.submission_work_manifest_path("missing-submission"),
            ):
                status, _, body = self.request("GET", path, token=token)
                self.assertEqual(status, 404, (role, path))
                self.assertEqual(body["code"], "not_found")

    def test_submission_work_review_ownership_transfer(self):
        ctx = self.course()
        submission_id = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            ctx["work_commit_id"],
            self.unique("submit"),
        )[2]["submission"]["id"]
        manifest_path = self.submission_work_manifest_path(submission_id)

        replacement = self.seed_user("teacher")
        replacement_token = self.login_token(replacement.username)
        status, _, _ = self.request(
            "PATCH",
            f"/api/v1/admin/classes/{ctx['class']['id']}/owner",
            {"teacherId": replacement.id},
            token=ctx["admin_token"],
        )
        self.assertEqual(status, 200)

        status, _, body = self.request(
            "GET", manifest_path, token=ctx["teacher_token"]
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))
        status, _, body = self.request(
            "GET", manifest_path, token=replacement_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["manifest"]["submissionId"], submission_id)

        # The submitting Student keeps access.
        status, _, _ = self.request(
            "GET", manifest_path, token=ctx["student_token"]
        )
        self.assertEqual(status, 200)

    def test_submission_work_review_rejects_query_and_bad_paths(self):
        ctx = self.course()
        submission_id = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            ctx["work_commit_id"],
            self.unique("submit"),
        )[2]["submission"]["id"]

        status, _, body = self.request(
            "GET",
            self.submission_work_manifest_path(submission_id) + "?x=1",
            token=ctx["student_token"],
        )
        self.assertEqual((status, body["code"]), (400, "invalid_request"))

        for encoded in (
            "..%2fsecret.py",
            "..%5csecret.py",
            "missing.py",
            "course%2fmodule%2fapp.py",
            ".picopg%2fwork.json",
            "%252e%252e",
        ):
            with self.subTest(encoded=encoded):
                status, _, body = self.request(
                    "GET",
                    self.submission_work_file_path(submission_id, encoded),
                    token=ctx["student_token"],
                )
                self.assertEqual(status, 404)
                self.assertEqual(body["code"], "not_found")

        for malformed in ("..", "bad%2fid", "missing-id"):
            with self.subTest(malformed=malformed):
                status, _, body = self.request(
                    "GET",
                    self.submission_work_manifest_path(malformed),
                    token=ctx["student_token"],
                )
                self.assertEqual(status, 404)
                self.assertEqual(body["code"], "not_found")


class CommentApiTest(unittest.TestCase):
    """HTTP contract for the immutable Comment API."""

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
            audit_output=io.StringIO(),
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

    def count_comments_for(self, submission_id):
        connection = open_database(self.database_path)
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM comments"
                " WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            return row["total"]
        finally:
            connection.close()

    def create_class(self, admin_token, teacher_id, name="Class"):
        status, _, body = self.request(
            "POST",
            "/api/v1/admin/classes",
            {"name": name, "teacherId": teacher_id},
            token=admin_token,
        )
        self.assertEqual(status, 201)
        return body["class"]

    def assign_student(self, admin_token, class_id, student_id):
        status, _, _ = self.request(
            "PUT",
            f"/api/v1/admin/classes/{class_id}/members/{student_id}",
            token=admin_token,
        )
        self.assertEqual(status, 200)

    def change_owner(self, admin_token, class_id, teacher_id):
        return self.request(
            "PATCH",
            f"/api/v1/admin/classes/{class_id}/owner",
            {"teacherId": teacher_id},
            token=admin_token,
        )

    def create_playground(self, token, playground_id):
        status, _, body = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": playground_id},
            token=token,
        )
        self.assertEqual(status, 201)
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

    def sync(
        self, token, playground_id, playground_commit_id, mapping, base=None
    ):
        files = [
            {"path": path, "data": base64.b64encode(data).decode("ascii")}
            for path, data in mapping.items()
        ]
        status, _, body = self.request(
            "POST",
            f"/api/v1/works/{playground_id}/sync",
            {
                "playgroundCommitId": playground_commit_id,
                "baseCommitId": base,
                "files": files,
            },
            token=token,
        )
        self.assertIn(status, (200, 201), body)
        return body["workCommitId"]

    def submit(self, token, release_id, work_commit_id, key):
        headers = {} if key is None else {"Idempotency-Key": key}
        return self.request(
            "POST",
            f"/api/v1/releases/{release_id}/submissions",
            {"workCommitId": work_commit_id},
            token=token,
            extra_headers=headers,
        )

    def post_comment(self, token, submission_id, payload, key):
        headers = {} if key is None else {"Idempotency-Key": key}
        return self.request(
            "POST",
            f"/api/v1/submissions/{submission_id}/comments",
            payload,
            token=token,
            extra_headers=headers,
        )

    def get_comments(self, token, submission_id):
        return self.request(
            "GET",
            f"/api/v1/submissions/{submission_id}/comments",
            token=token,
        )

    def get_submission(self, token, submission_id):
        return self.request(
            "GET",
            f"/api/v1/submissions/{submission_id}",
            token=token,
        )

    def course(self):
        """Return an Admin/Teacher/Student course with one Submission."""

        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        student = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        teacher_token = self.login_token(teacher.username)
        student_token = self.login_token(student.username)
        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        commit_id = self.publish(admin_token, playground_id)
        klass = self.create_class(admin_token, teacher.id)
        self.assign_student(admin_token, klass["id"], student.id)
        release = self.post_release(
            teacher_token,
            klass["id"],
            self.unique("rel"),
            self.release_payload(playground_id, commit_id),
        )[2]["release"]
        work_commit_id = self.sync(
            student_token,
            playground_id,
            commit_id,
            {"work/main.py": b"print('work')"},
        )
        submission = self.submit(
            student_token,
            release["id"],
            work_commit_id,
            self.unique("submit"),
        )[2]["submission"]
        return {
            "admin": admin,
            "teacher": teacher,
            "student": student,
            "admin_token": admin_token,
            "teacher_token": teacher_token,
            "student_token": student_token,
            "class": klass,
            "playground_id": playground_id,
            "commit_id": commit_id,
            "release": release,
            "work_commit_id": work_commit_id,
            "submission": submission,
        }

    def raw_status(self, method, path, payload=None):
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response.read()
        status = response.status
        connection.close()
        return status

    def test_create_list_and_exact_retry(self):
        ctx = self.course()
        submission_id = ctx["submission"]["id"]

        # The owning Student sees an empty history before any feedback.
        status, _, listed = self.get_comments(
            ctx["student_token"], submission_id
        )
        self.assertEqual(status, 200)
        self.assertEqual(listed["comments"], [])

        key = self.unique("comment")
        status, _, body = self.post_comment(
            ctx["teacher_token"], submission_id, {"body": "Nice work"}, key
        )
        self.assertEqual(status, 201)
        record = body["comment"]
        self.assertEqual(
            set(record),
            {
                "id",
                "submissionId",
                "authorUserId",
                "source",
                "body",
                "createdAt",
            },
        )
        self.assertEqual(record["submissionId"], submission_id)
        self.assertEqual(record["authorUserId"], ctx["teacher"].id)
        self.assertEqual(record["source"], "teacher")
        self.assertEqual(record["body"], "Nice work")
        self.assertTrue(record["createdAt"])
        # The metadata response never contains Work source.
        self.assertNotIn("files", record)
        self.assertNotIn("content", record)

        # An exact retry replays the stored result without a duplicate row.
        retry_status, _, retry = self.post_comment(
            ctx["teacher_token"], submission_id, {"body": "Nice work"}, key
        )
        self.assertEqual(retry_status, 201)
        self.assertEqual(retry, body)
        self.assertEqual(self.count_comments_for(submission_id), 1)

        # The owning Student and the owning Teacher see the same list.
        for token in (ctx["student_token"], ctx["teacher_token"]):
            status, _, listed = self.get_comments(token, submission_id)
            self.assertEqual(status, 200)
            self.assertEqual(listed["comments"], [record])

    def test_changed_body_under_same_key_conflicts(self):
        ctx = self.course()
        submission_id = ctx["submission"]["id"]
        key = self.unique("comment")

        self.assertEqual(
            self.post_comment(
                ctx["teacher_token"], submission_id, {"body": "one"}, key
            )[0],
            201,
        )
        status, _, body = self.post_comment(
            ctx["teacher_token"], submission_id, {"body": "two"}, key
        )
        self.assertEqual((status, body["code"]), (409, "idempotency_conflict"))
        self.assertEqual(self.count_comments_for(submission_id), 1)

    def test_caller_selected_author_or_source_is_rejected(self):
        ctx = self.course()
        submission_id = ctx["submission"]["id"]

        for spoof in (
            {"body": "hi", "source": "ai"},
            {"body": "hi", "source": "system"},
            {"body": "hi", "source": "teacher"},
            {"body": "hi", "authorUserId": ctx["student"].id},
            {"body": "hi", "authorUserId": None},
            {
                "body": "hi",
                "source": "ai",
                "authorUserId": ctx["teacher"].id,
            },
        ):
            status, _, body = self.post_comment(
                ctx["teacher_token"], submission_id, spoof, self.unique("key")
            )
            self.assertEqual(
                (status, body["code"]), (400, "invalid_request"), spoof
            )
        self.assertEqual(self.count_comments_for(submission_id), 0)

        # A body-only request still records the actor as the server-owned
        # Teacher author.
        status, _, body = self.post_comment(
            ctx["teacher_token"],
            submission_id,
            {"body": "ok"},
            self.unique("key"),
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["comment"]["source"], "teacher")
        self.assertEqual(body["comment"]["authorUserId"], ctx["teacher"].id)

    def test_read_and_create_authorization(self):
        ctx = self.course()
        submission_id = ctx["submission"]["id"]
        self.assertEqual(
            self.post_comment(
                ctx["teacher_token"],
                submission_id,
                {"body": "feedback"},
                self.unique("key"),
            )[0],
            201,
        )

        # A Student may never create feedback.
        status, _, body = self.post_comment(
            ctx["student_token"],
            submission_id,
            {"body": "x"},
            self.unique("key"),
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))

        # Another Student, even in the same Class, cannot read the history.
        other = self.seed_user("student")
        self.assign_student(ctx["admin_token"], ctx["class"]["id"], other.id)
        other_token = self.login_token(other.username)
        status, _, body = self.get_comments(other_token, submission_id)
        self.assertEqual((status, body["code"]), (404, "not_found"))

        # An unrelated Teacher can neither read nor create.
        outsider = self.seed_user("teacher")
        outsider_token = self.login_token(outsider.username)
        status, _, body = self.get_comments(outsider_token, submission_id)
        self.assertEqual((status, body["code"]), (404, "not_found"))
        status, _, body = self.post_comment(
            outsider_token, submission_id, {"body": "x"}, self.unique("key")
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))

        # Admin management access is not comment body access.
        status, _, body = self.get_comments(ctx["admin_token"], submission_id)
        self.assertEqual((status, body["code"]), (403, "forbidden"))
        status, _, body = self.post_comment(
            ctx["admin_token"],
            submission_id,
            {"body": "x"},
            self.unique("key"),
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))

        # Missing and foreign submissions are hidden behind the same 404.
        missing = uuid.uuid4().hex
        status, _, body = self.get_comments(ctx["teacher_token"], missing)
        self.assertEqual((status, body["code"]), (404, "not_found"))
        status, _, body = self.post_comment(
            ctx["teacher_token"], missing, {"body": "x"}, self.unique("key")
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

    def test_owner_transfer_preserves_submission_and_old_comments(self):
        ctx = self.course()
        submission_id = ctx["submission"]["id"]
        first = self.post_comment(
            ctx["teacher_token"],
            submission_id,
            {"body": "from first teacher"},
            self.unique("key"),
        )[2]["comment"]

        new_teacher = self.seed_user("teacher")
        status, _, _ = self.change_owner(
            ctx["admin_token"], ctx["class"]["id"], new_teacher.id
        )
        self.assertEqual(status, 200)
        new_token = self.login_token(new_teacher.username)

        # The old Teacher loses both read and create access.
        status, _, body = self.get_comments(
            ctx["teacher_token"], submission_id
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))
        status, _, body = self.post_comment(
            ctx["teacher_token"],
            submission_id,
            {"body": "x"},
            self.unique("key"),
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))

        # The new Teacher sees the old comment and can append to the chain.
        status, _, listed = self.get_comments(new_token, submission_id)
        self.assertEqual(status, 200)
        self.assertEqual(listed["comments"], [first])
        status, _, body = self.post_comment(
            new_token,
            submission_id,
            {"body": "from new teacher"},
            self.unique("key"),
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["comment"]["authorUserId"], new_teacher.id)

        # The transfer does not change the Submission or its history.
        status, _, body = self.get_submission(new_token, submission_id)
        self.assertEqual(status, 200)
        self.assertEqual(body["submission"], ctx["submission"])
        status, _, listed = self.get_comments(
            ctx["student_token"], submission_id
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            sorted(item["body"] for item in listed["comments"]),
            ["from first teacher", "from new teacher"],
        )
        self.assertEqual(self.count_comments_for(submission_id), 2)

    def test_body_and_idempotency_key_limits(self):
        ctx = self.course()
        submission_id = ctx["submission"]["id"]
        path = f"/api/v1/submissions/{submission_id}/comments"

        # A missing, blank, or oversized idempotency key is rejected before the
        # body is read.
        for headers in (
            {},
            {"Idempotency-Key": "  "},
            {"Idempotency-Key": "k" * 256},
        ):
            status, _, body = self.request(
                "POST",
                path,
                {"body": "hi"},
                token=ctx["teacher_token"],
                extra_headers=headers,
            )
            self.assertEqual((status, body["code"]), (400, "invalid_request"))

        # A missing or malformed body is an invalid request.
        for invalid in ({}, {"body": ""}, {"body": None}, {"body": 7}):
            status, _, body = self.post_comment(
                ctx["teacher_token"],
                submission_id,
                invalid,
                self.unique("key"),
            )
            self.assertEqual(
                (status, body["code"]), (400, "invalid_request"), invalid
            )
        self.assertEqual(self.count_comments_for(submission_id), 0)

        # The full Comment-domain body ceiling is accepted over HTTP.
        exact = "x" * MAX_COMMENT_BODY_BYTES
        status, _, body = self.post_comment(
            ctx["teacher_token"],
            submission_id,
            {"body": exact},
            self.unique("key"),
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["comment"]["body"], exact)

        # One byte past the ceiling is rejected by the domain, not by a
        # transport cap, and stores nothing.
        over = "x" * (MAX_COMMENT_BODY_BYTES - 1) + "\u00e9"
        status, _, body = self.post_comment(
            ctx["teacher_token"],
            submission_id,
            {"body": over},
            self.unique("key"),
        )
        self.assertEqual((status, body["code"]), (400, "invalid_request"))
        self.assertEqual(self.count_comments_for(submission_id), 1)

        # A JSON envelope beyond the transport cap is rejected before reading.
        huge = "x" * (MAX_COMMENT_JSON_BODY_BYTES + 1)
        status, _, body = self.post_comment(
            ctx["teacher_token"],
            submission_id,
            {"body": huge},
            self.unique("key"),
        )
        self.assertEqual((status, body["code"]), (413, "request_too_large"))

    def test_authentication_is_required(self):
        ctx = self.course()
        submission_id = ctx["submission"]["id"]
        calls = (
            self.get_comments(None, submission_id),
            self.get_comments("bogus", submission_id),
            self.post_comment(None, submission_id, {"body": "x"}, "key"),
            self.post_comment("bogus", submission_id, {"body": "x"}, "key"),
        )
        for status, _, body in calls:
            self.assertEqual((status, body["code"]), (401, "invalid_session"))

    def test_route_methods_and_paths(self):
        submission_id = self.unique("sub")
        comment_path = f"/api/v1/submissions/{submission_id}/comments"

        # There is no mutable or AI/system public route.
        for method in ("PUT", "PATCH", "DELETE"):
            status, headers, body = self.request(method, comment_path)
            self.assertEqual(status, 405, method)
            self.assertEqual(headers.get("allow"), "GET, HEAD, POST", method)
            self.assertEqual(body["code"], "method_not_allowed")

        # The internal AI/system endpoint is not a public route.
        for method in ("GET", "POST"):
            status = self.raw_status(
                method,
                "/internal/v1/comments",
                {"source": "ai", "body": "x"},
            )
            self.assertEqual(status, 404, method)

        # A malformed comment path is not a route.
        for method, path in (
            ("GET", "/api/v1/submissions//comments"),
            ("GET", f"/api/v1/submissions/{submission_id}/comments/extra"),
            ("POST", f"/api/v1/submissions/{submission_id}/comments/extra"),
        ):
            status, _, body = self.request(method, path)
            self.assertEqual(
                (status, body["code"]), (404, "not_found"), (method, path)
            )


class _AuditClock:
    """Monotonic fake clock: every call advances a fixed step."""

    def __init__(self, start=None, step_ms=5):
        self._current = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._step = timedelta(milliseconds=step_ms)

    def __call__(self):
        moment = self._current
        self._current = moment + self._step
        return moment


class AuditApiTest(unittest.TestCase):
    """HTTP contract for bounded structured request auditing."""

    MANIFEST = b'{"schemaVersion":1,"runtime":"python"}'

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        base = Path(self.temporary_directory.name)
        self.database_path = base / "app.db"
        self.output = io.StringIO()
        self._generated = 0

        def next_request_id():
            self._generated += 1
            return f"generated-{self._generated}"

        self.server = make_server(
            host="127.0.0.1",
            port=0,
            database_path=self.database_path,
            repository_path=base / "picopg.git",
            audit_output=self.output,
            audit_clock=_AuditClock(),
            audit_request_ids=next_request_id,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary_directory.cleanup()

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

    def raw_request(self, method, path):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path)
        response = connection.getresponse()
        response.read()
        status = response.status
        connection.close()
        return status

    def audit_events(self, minimum=0, timeout=2):
        """Read parsed events, waiting for the sink to catch up if needed."""

        deadline = time.monotonic() + timeout
        while True:
            lines = [
                line for line in self.output.getvalue().splitlines() if line
            ]
            events = [json.loads(line) for line in lines]
            if len(events) >= minimum or time.monotonic() > deadline:
                return events
            time.sleep(0.005)

    def _seed(self, username, role):
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

    def seed_admin(self, username):
        return self._seed(username, "admin")

    def seed_user(self, role):
        return self._seed(f"{role}-{uuid.uuid4().hex[:8]}", role)

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

    def build_zip(self, entries=None):
        if entries is None:
            entries = (
                ("picopg.json", self.MANIFEST),
                ("course/module/app.py", b"print(1)"),
            )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return buffer.getvalue()

    def unique(self, prefix):
        return f"{prefix}-{uuid.uuid4().hex[:8]}"

    def set_policy(self, token, playground_id, policy):
        status, _, body = self.request(
            "PATCH",
            f"/api/v1/playgrounds/{playground_id}",
            {"policy": policy},
            token=token,
        )
        self.assertEqual(status, 200)
        return body["playground"]

    def publish(self, token, playground_id, entries=None):
        status, _, body = self.request(
            "POST",
            f"/api/v1/playgrounds/{playground_id}/commits",
            raw_body=self.build_zip(entries),
            token=token,
            content_type="application/zip",
            extra_headers={"Idempotency-Key": self.unique("key")},
        )
        self.assertEqual(status, 201)
        return body["commitId"]

    def sync(self, token, playground_id, playground_commit_id, mapping, base=None):
        return self.request(
            "POST",
            f"/api/v1/works/{playground_id}/sync",
            {
                "playgroundCommitId": playground_commit_id,
                "baseCommitId": base,
                "files": [
                    {"path": path, "data": base64.b64encode(data).decode("ascii")}
                    for path, data in mapping.items()
                ],
            },
            token=token,
        )

    def manifest_path(self, playground_id, commit_id):
        return (
            f"/api/v1/playgrounds/{playground_id}/commits/{commit_id}/manifest"
        )

    def file_path(self, playground_id, commit_id, path):
        return (
            f"/api/v1/playgrounds/{playground_id}"
            f"/commits/{commit_id}/files/{path}"
        )

    def test_successful_mutation_is_audited_with_request_id(self):
        admin = self.seed_admin("audit-admin")
        token = self.login_token(admin.username)

        status, headers, _ = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": "pg-audit"},
            token=token,
        )
        self.assertEqual(status, 201)
        request_id = headers["x-request-id"]
        self.assertTrue(request_id)

        # Register, login, and the direct-DB seed emit nothing, so the one
        # mutation is the only event.
        events = self.audit_events(minimum=1)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["requestId"], request_id)
        self.assertEqual(event["actorId"], admin.id)
        self.assertEqual(event["action"], "playground.create")
        self.assertIsNone(event["target"])
        self.assertEqual(event["status"], 201)
        self.assertIsNone(event["code"])
        self.assertGreaterEqual(event["durationMs"], 0)
        self.assertTrue(event["time"].endswith("Z"))

    def test_publish_records_target_without_body_or_secrets(self):
        admin = self.seed_admin("audit-admin")
        token = self.login_token(admin.username)
        playground_id = "pg-audit-publish"
        self.create_playground(token, playground_id)

        secret = "SUPER-SECRET-SOURCE-FRAGMENT"
        body = self.build_zip(
            (
                ("picopg.json", self.MANIFEST),
                ("course/module/app.py", secret.encode("utf-8")),
            )
        )
        status, _, _ = self.request(
            "POST",
            f"/api/v1/playgrounds/{playground_id}/commits",
            raw_body=body,
            token=token,
            content_type="application/zip",
            extra_headers={"Idempotency-Key": "key-1"},
        )
        self.assertEqual(status, 201)

        events = self.audit_events(minimum=2)
        publish = events[-1]
        self.assertEqual(publish["action"], "playground.publish")
        self.assertEqual(publish["target"], playground_id)
        self.assertEqual(publish["actorId"], admin.id)
        self.assertEqual(publish["status"], 201)

        text = self.output.getvalue()
        for secret_text in (secret, token, "secret", "Authorization", "Idempotency-Key"):
            self.assertNotIn(secret_text, text)
        self.assertNotIn(secret, str(events))

    def test_retry_is_audited_once_per_request(self):
        admin = self.seed_admin("audit-admin")
        token = self.login_token(admin.username)
        playground_id = "pg-audit-retry"
        self.create_playground(token, playground_id)
        body = self.build_zip()

        def publish():
            return self.request(
                "POST",
                f"/api/v1/playgrounds/{playground_id}/commits",
                raw_body=body,
                token=token,
                content_type="application/zip",
                extra_headers={"Idempotency-Key": "key-retry"},
            )

        self.assertEqual(publish()[0], 201)
        self.assertEqual(publish()[0], 201)

        events = [
            item
            for item in self.audit_events(minimum=3)
            if item["action"] == "playground.publish"
        ]
        self.assertEqual(len(events), 2)
        self.assertEqual([item["status"] for item in events], [201, 201])
        self.assertNotEqual(
            events[0]["requestId"], events[1]["requestId"]
        )

    def test_forbidden_mutation_records_actor_target_and_code(self):
        student = self.seed_user("student")
        token = self.login_token(student.username)

        status, _, body = self.request(
            "PATCH",
            f"/api/v1/admin/users/{student.id}/role",
            {"role": "admin"},
            token=token,
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))

        events = self.audit_events(minimum=1)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["action"], "admin.user.role.update")
        self.assertEqual(event["actorId"], student.id)
        self.assertEqual(event["target"], student.id)
        self.assertEqual(event["status"], 403)
        self.assertEqual(event["code"], "forbidden")

    def test_invalid_session_on_mutation_is_audited(self):
        status, _, body = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": "pg-denied"},
            token="bogus-token",
        )
        self.assertEqual((status, body["code"]), (401, "invalid_session"))

        events = self.audit_events(minimum=1)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["action"], "playground.create")
        self.assertIsNone(event["actorId"])
        self.assertIsNone(event["target"])
        self.assertEqual(event["status"], 401)
        self.assertEqual(event["code"], "invalid_session")

    def test_denied_read_is_audited_without_target(self):
        status, _, body = self.request("GET", "/api/v1/me")
        self.assertEqual((status, body["code"]), (401, "invalid_session"))

        events = self.audit_events(minimum=1)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["action"], "request.denied")
        self.assertIsNone(event["actorId"])
        self.assertIsNone(event["target"])
        self.assertEqual(event["status"], 401)

    def test_authenticated_logout_records_actor(self):
        admin = self.seed_admin("audit-logout")
        token = self.login_token(admin.username)

        status, _, body = self.request("POST", "/api/v1/logout", token=token)
        self.assertEqual(status, 204)
        self.assertIsNone(body)

        events = self.audit_events(minimum=1)
        logout = [
            event
            for event in events
            if event["action"] == "account.logout"
        ]
        self.assertEqual(len(logout), 1)
        self.assertEqual(logout[0]["actorId"], admin.id)
        self.assertEqual(logout[0]["status"], 204)

    def test_unauthenticated_logout_records_null_actor(self):
        # No session at all: the request is rejected but still audited.
        status, _, body = self.request("POST", "/api/v1/logout")
        self.assertEqual((status, body["code"]), (401, "invalid_session"))

        # An invalid token still succeeds (idempotent logout) and is audited.
        invalid_status, _, _ = self.request(
            "POST", "/api/v1/logout", token="bogus-token"
        )
        self.assertEqual(invalid_status, 204)

        events = self.audit_events(minimum=2)
        logout = [
            event
            for event in events
            if event["action"] == "account.logout"
        ]
        self.assertEqual(len(logout), 2)
        self.assertTrue(all(event["actorId"] is None for event in logout))
        self.assertEqual([event["status"] for event in logout], [401, 204])

    def test_route_exception_never_writes_traceback_to_audit(self):
        capture = io.StringIO()
        original_output = self.server.audit._output
        self.server.audit._output = capture

        def explode(*args, **kwargs):
            raise RuntimeError("injected route failure")

        try:
            with mock.patch.object(server.Handler, "_get_me", explode):
                with contextlib.redirect_stderr(capture):
                    try:
                        self.request("GET", "/api/v1/me")
                    except Exception:
                        pass
                    deadline = time.monotonic() + 2
                    while (
                        "request.error" not in capture.getvalue()
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.005)
        finally:
            self.server.audit._output = original_output

        text = capture.getvalue()
        self.assertNotIn("Traceback (most recent call last)", text)
        self.assertNotIn("injected route failure", text)
        self.assertNotIn('File "', text)

        # The failed request terminates and the server keeps serving.
        self.assertEqual(self.raw_request("GET", "/healthz"), 200)

    def test_reads_and_account_routes_are_not_audited(self):
        self.raw_request("GET", "/healthz")
        self.raw_request("GET", "/")
        self.request(
            "POST",
            "/api/v1/register",
            {"username": "public-student", "password": "secret"},
        )
        admin = self.seed_admin("audit-admin")
        token = self.login_token(admin.username)
        self.create_playground(token, "pg-quiet")

        events = self.audit_events(minimum=1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "playground.create")

    def test_client_request_id_is_echoed_and_unsafe_ids_replaced(self):
        admin = self.seed_admin("audit-admin")
        token = self.login_token(admin.username)

        status, headers, _ = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": "pg-client-id"},
            token=token,
            extra_headers={"X-Request-Id": "client-id-123"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(headers["x-request-id"], "client-id-123")

        status, headers, _ = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": "pg-bad-id"},
            token=token,
            extra_headers={"X-Request-Id": "bad id"},
        )
        self.assertEqual(status, 201)
        generated = headers["x-request-id"]
        self.assertTrue(generated.startswith("generated-"))

        events = self.audit_events(minimum=2)
        self.assertEqual(
            [item["requestId"] for item in events],
            ["client-id-123", generated],
        )
        self.assertNotIn("bad id", self.output.getvalue())

    def test_control_characters_in_supplied_id_are_replaced(self):
        admin = self.seed_admin("audit-admin")
        token = self.login_token(admin.username)

        status, headers, _ = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": "pg-control"},
            token=token,
            extra_headers={"X-Request-Id": "evil\x01id"},
        )
        self.assertEqual(status, 201)
        generated = headers["x-request-id"]
        self.assertNotIn("\x01", generated)
        self.assertTrue(generated.startswith("generated-"))

        events = self.audit_events(minimum=1)
        self.assertEqual(events[0]["requestId"], generated)
        self.assertNotIn("evil", self.output.getvalue())

    def test_audit_sink_failure_does_not_change_response(self):
        admin = self.seed_admin("audit-admin")
        token = self.login_token(admin.username)

        class BrokenSink:
            def write(self, line):
                raise OSError("audit sink down")

            def flush(self):
                raise OSError("audit sink down")

        self.server.audit._output = BrokenSink()
        status, headers, _ = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": "pg-boom"},
            token=token,
        )
        self.assertEqual(status, 201)
        self.assertTrue(headers["x-request-id"])

        # The business write landed exactly once, so a duplicate is rejected
        # instead of being retried because auditing failed.
        retry_status, _, retry_body = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": "pg-boom"},
            token=token,
        )
        self.assertEqual((retry_status, retry_body["code"]), (409, "conflict"))

    def test_denied_private_content_read_is_audited_opaquely(self):
        admin = self.seed_admin("audit-content-admin")
        token = self.login_token(admin.username)
        playground_id = self.unique("pg-private")
        self.create_playground(token, playground_id)
        # No policy is set, so both the manifest and its files stay private.
        commit_id = self.publish(token, playground_id)

        status, _, body = self.request(
            "GET", self.manifest_path(playground_id, commit_id)
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

        secret_name = "private-notes-secret.txt"
        status, _, body = self.request(
            "GET",
            self.file_path(
                playground_id, commit_id, f"course/{secret_name}"
            ),
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

        denied = [
            event
            for event in self.audit_events(minimum=4)
            if event["code"] == "content_denied"
        ]
        self.assertEqual(len(denied), 2)
        for event in denied:
            self.assertEqual(event["action"], "content.read")
            self.assertEqual(event["status"], 404)
            self.assertEqual(event["target"], f"{playground_id}/{commit_id}")

        text = self.output.getvalue()
        self.assertNotIn(secret_name, text)
        self.assertNotIn("course/", text)
        self.assertNotIn(token, text)

    def test_work_head_changed_conflict_is_audited_with_code(self):
        admin = self.seed_admin("audit-sync-admin")
        admin_token = self.login_token(admin.username)
        student = self.seed_user("student")
        student_token = self.login_token(student.username)
        playground_id = self.unique("pg-sync")
        self.create_playground(admin_token, playground_id)
        self.set_policy(admin_token, playground_id, "public")
        commit_id = self.publish(admin_token, playground_id)

        status, _, body = self.sync(
            student_token,
            playground_id,
            commit_id,
            {"work/main.py": b"print(1)"},
        )
        self.assertEqual(status, 201)

        # Resending without the canonical base is a CAS conflict, and the
        # response carries no audit code unless the route sets one explicitly.
        status, _, body = self.sync(
            student_token,
            playground_id,
            commit_id,
            {"work/main.py": b"print(1)"},
        )
        self.assertEqual((status, body["code"]), (409, "work_head_changed"))

        conflicts = [
            event
            for event in self.audit_events(minimum=5)
            if event["status"] == 409
        ]
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict["action"], "work.sync")
        self.assertEqual(conflict["target"], playground_id)
        self.assertEqual(conflict["actorId"], student.id)
        self.assertEqual(conflict["code"], "work_head_changed")

    def test_token_shaped_request_id_is_not_echoed(self):
        admin = self.seed_admin("audit-token-id")
        token = self.login_token(admin.username)

        status, headers, _ = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": self.unique("pg-token-id")},
            token=token,
            extra_headers={"X-Request-Id": token},
        )
        self.assertEqual(status, 201)
        echoed = headers["x-request-id"]
        self.assertNotEqual(echoed, token)
        self.assertTrue(echoed.startswith("generated-"))

        events = self.audit_events(minimum=1)
        self.assertEqual(events[0]["requestId"], echoed)
        self.assertNotIn(token, self.output.getvalue())

    def test_failed_response_write_is_audited_once_as_failure(self):
        class FailingBodyWriter:
            """Let the response headers out, then break on the body write."""

            def __init__(self, stream):
                self._stream = stream

            def write(self, data):
                if data.startswith(b"HTTP/"):
                    return self._stream.write(data)
                raise BrokenPipeError("client disconnected")

            def flush(self):
                return self._stream.flush()

        admin = self.seed_admin("audit-write-admin")
        token = self.login_token(admin.username)

        original_setup = server.Handler.setup

        def failing_setup(handler_self):
            original_setup(handler_self)
            handler_self.wfile = FailingBodyWriter(handler_self.wfile)

        with mock.patch.object(server.Handler, "setup", failing_setup):
            connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
            connection.request(
                "POST",
                "/api/v1/playgrounds",
                body=json.dumps({"id": self.unique("pg-write")}).encode(
                    "utf-8"
                ),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
            response = connection.getresponse()
            request_id = response.getheader("X-Request-Id")
            try:
                response.read()
            except Exception:
                pass
            connection.close()

        # The client received the request id, and the one audit line records
        # the failed write rather than a false success.
        self.assertTrue(request_id)
        events = self.audit_events(minimum=1)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["requestId"], request_id)
        self.assertEqual(event["action"], "request.error")
        self.assertEqual(event["status"], 500)
        self.assertEqual(event["code"], "internal_error")

    def test_error_after_completed_response_is_not_audited_twice(self):
        admin = self.seed_admin("audit-double-admin")
        token = self.login_token(admin.username)

        def respond_then_raise(handler_self):
            handler_self._read_json()
            handler_self._respond_json(201, {"playground": {"id": "pg-x"}})
            raise RuntimeError("injected failure after response")

        with mock.patch.object(
            server.Handler, "_post_playground", respond_then_raise
        ):
            status, _, _ = self.request(
                "POST",
                "/api/v1/playgrounds",
                {"id": self.unique("pg-double")},
                token=token,
            )
        self.assertEqual(status, 201)

        events = self.audit_events(minimum=1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "playground.create")
        self.assertEqual(events[0]["status"], 201)

    def test_authorized_missing_file_read_is_audited_not_found(self):
        admin = self.seed_admin("audit-missing-admin")
        token = self.login_token(admin.username)
        playground_id = self.unique("pg-missing-file")
        self.create_playground(token, playground_id)
        self.set_policy(token, playground_id, "public")
        commit_id = self.publish(token, playground_id)

        secret_name = "absent-file-secret.txt"
        status, _, body = self.request(
            "GET",
            self.file_path(
                playground_id, commit_id, f"course/{secret_name}"
            ),
            token=token,
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

        content_reads = [
            event
            for event in self.audit_events(minimum=4)
            if event["action"] == "content.read"
        ]
        self.assertEqual(len(content_reads), 1)
        event = content_reads[0]
        self.assertEqual(event["code"], "not_found")
        self.assertEqual(event["status"], 404)
        self.assertEqual(event["target"], f"{playground_id}/{commit_id}")

        text = self.output.getvalue()
        self.assertNotIn(secret_name, text)
        self.assertNotIn("course/", text)
        self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
