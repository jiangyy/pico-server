"""End-to-end wire contract test for the Client ↔ Server boundary.

The Server's HTTP shape is verified two ways: the per-endpoint unit tests in
``test_api.py`` already pin every code path, and this file proves that the
same shape holds when a real bundle is published and read back through the
same endpoints the Web adapter (``apps/picopg-web/adapter.ts``) actually
calls.

Three things are pinned here and only here:

1. The new teacher-facing resource endpoint ``GET /api/v1/playgrounds`` returns
   the exact ``{id, commitId, title?, summary?, requiresSubmission}`` shape
   that ``readTeacherResources`` in ``adapter.ts`` projects via
   ``readPaginatedCollection``.
2. A bundle published through the public ``POST /api/v1/playgrounds/{id}/commits``
   ZIP path appears in that resource list with the right metadata, so the
   teacher resource pool is never empty for content that has actually shipped.
3. Pagination, role authorization, and archived-exclusion all hold against the
   real bundle ingest path, not only against the synthetic seed used by the
   per-endpoint tests.

Anything not listed above is intentionally covered by the existing unit
suites; this file does not duplicate them.
"""

import http.client
import io
import json
import socket
import threading
import time
import unittest
import urllib.parse
import zipfile
from pathlib import Path

import accounts
from database import open_database
from server import HOST, make_server


def _free_port() -> int:
    """Bind port 0 to ask the kernel for an unused TCP port, then release it."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def _wait_for_server(port: int, deadline_seconds: float = 5.0) -> None:
    """Poll ``/api/v1/bootstrap`` until the Server responds or the deadline."""

    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            connection = http.client.HTTPConnection(HOST, port, timeout=1)
            connection.request("GET", "/api/v1/bootstrap")
            response = connection.getresponse()
            response.read()
            connection.close()
            return
        except (ConnectionRefusedError, OSError):
            time.sleep(0.05)
    raise RuntimeError("Server did not start within the deadline")


class WireIntegrationTest(unittest.TestCase):
    """Real Server, real HTTP, real bundles.

    Each test method gets a fresh data directory and a fresh Server so no
    state leaks between cases. The setup cost is acceptable because every
    Server instance already finishes initialization in well under a second
    on a warm filesystem.
    """

    def setUp(self):
        from tempfile import TemporaryDirectory

        self.temporary_directory = TemporaryDirectory()
        base = Path(self.temporary_directory.name)
        self.database_path = base / "app.db"
        self.repository_path = base / "picopg.git"

        self.port = _free_port()
        self.server = make_server(
            host=HOST,
            port=self.port,
            database_path=self.database_path,
            repository_path=self.repository_path,
            audit_output=io.StringIO(),
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        _wait_for_server(self.port)

        # Seed two managed accounts through the same DB write path the
        # Server itself uses. Public ``register`` is reserved for students;
        # admin and teacher accounts have no public creation endpoint, and
        # operators do this through the same code path.
        suffix = str(id(self))
        self.admin_username = "wire-admin-" + suffix
        self.admin_password = "wire-admin-pass-" + suffix
        self.teacher_username = "wire-teacher-" + suffix
        self.teacher_password = "wire-teacher-pass-" + suffix
        actor = accounts.User(
            id="seed-actor",
            username="seed-actor",
            role="admin",
            created_at="2026-01-01T00:00:00Z",
        )
        connection = open_database(self.database_path)
        try:
            with connection:
                accounts.create_user(
                    connection,
                    actor,
                    self.admin_username,
                    self.admin_password,
                    "admin",
                )
                accounts.create_user(
                    connection,
                    actor,
                    self.teacher_username,
                    self.teacher_password,
                    "teacher",
                )
        finally:
            connection.close()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary_directory.cleanup()

    def _connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(HOST, self.port, timeout=5)

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload=None,
        token: str | None = None,
        raw_body: bytes | None = None,
        content_type: str = "application/json",
        extra_headers: dict | None = None,
    ):
        headers: dict = {}
        body = raw_body
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        if raw_body is not None or payload is not None:
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(len(body))
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if extra_headers:
            headers.update(extra_headers)

        connection = self._connection()
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            decoded = json.loads(raw) if raw else None
            return response.status, dict(response.getheaders()), decoded
        finally:
            connection.close()

    def _raw_request(self, method: str, path: str, *, headers=None):
        connection = self._connection()
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            body = response.read()
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    def _login(self, username: str, password: str) -> str:
        status, headers, body = self._request(
            "POST",
            "/api/v1/login",
            payload={"username": username, "password": password},
        )
        self.assertEqual(status, 200, body)
        self.assertIn("sessionToken", body)
        self.assertEqual(body["user"]["username"], username)
        self.assertIn("Set-Cookie", headers)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return body["sessionToken"]

    def _admin_token(self) -> str:
        return self._login(self.admin_username, self.admin_password)

    def _teacher_token(self) -> str:
        return self._login(self.teacher_username, self.teacher_password)

    def _register_student(self, username: str, password: str) -> dict:
        status, _, body = self._request(
            "POST",
            "/api/v1/register",
            payload={"username": username, "password": password},
        )
        self.assertEqual(status, 201, body)
        return body["user"]

    def _student_token(self, username: str, password: str) -> str:
        self._register_student(username, password)
        return self._login(username, password)

    def _create_playground(self, admin_token: str, playground_id: str) -> None:
        status, _, body = self._request(
            "POST",
            "/api/v1/playgrounds",
            payload={"id": playground_id},
            token=admin_token,
        )
        self.assertEqual(status, 201, body)

    def _build_zip(self, entries) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return buffer.getvalue()

    def _publish(
        self,
        admin_token: str,
        playground_id: str,
        zip_body: bytes,
        idempotency_key: str,
    ) -> dict:
        status, _, body = self._request(
            "POST",
            f"/api/v1/playgrounds/{urllib.parse.quote(playground_id, safe='')}/commits",
            raw_body=zip_body,
            token=admin_token,
            content_type="application/zip",
            extra_headers={"Idempotency-Key": idempotency_key},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(
            set(body.keys()), {"playgroundId", "commitId", "noOp"}
        )
        return body

    # --- Tests ----------------------------------------------------------

    def test_published_bundle_appears_in_teacher_resource_pool(self) -> None:
        """End-to-end: register a student via the public API, publish a real
        ZIP, then read it back through the new teacher resource endpoint and
        the admin discovery endpoint. Verifies the wire shape matches what
        ``adapter.ts`` projects."""

        playground_id = "wire-hanoi"
        title = "汉诺塔 / Tower of Hanoi"
        summary = "Move the tower, one disk at a time."
        manifest = json.dumps(
            {
                "schemaVersion": 1,
                "runtime": "python",
                "title": title,
                "summary": summary,
                "requiresSubmission": True,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        course_module = b"print('hanoi')\n"
        zip_body = self._build_zip(
            [
                ("picopg.json", manifest),
                ("course/module/app.py", course_module),
            ]
        )

        admin_token = self._admin_token()
        teacher_token = self._teacher_token()
        self._student_token("wire-student-" + str(id(self)), "wire-student-pass")

        self._create_playground(admin_token, playground_id)
        published = self._publish(
            admin_token, playground_id, zip_body, "key-hanoi"
        )
        self.assertEqual(published["playgroundId"], playground_id)
        self.assertFalse(published["noOp"])
        self.assertEqual(len(published["commitId"]), 40)  # sha-1 hex

        # Teacher view: the published Playground must appear in the resource
        # pool with the exact metadata shape ``adapter.ts`` projects.
        status, _, body = self._request(
            "GET", "/api/v1/playgrounds", token=teacher_token
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body.keys()), {"playgrounds", "nextCursor"})
        match = next(
            item for item in body["playgrounds"] if item["id"] == playground_id
        )
        self.assertEqual(
            match,
            {
                "id": playground_id,
                "commitId": published["commitId"],
                "title": title,
                "summary": summary,
                "requiresSubmission": True,
            },
        )

        # Admin discovery endpoint carries a different shape (``playgroundId``,
        # ``headCommitId``, ``archived``) than the teacher resource pool.
        # This is the second public list shape the Server exposes for the
        # same Playground metadata, and it must round-trip end-to-end too.
        status, _, body = self._request(
            "GET", "/api/v1/admin/playgrounds", token=admin_token
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body.keys()), {"playgrounds", "nextCursor"})
        admin_item = next(
            item for item in body["playgrounds"] if item["playgroundId"] == playground_id
        )
        self.assertEqual(admin_item["headCommitId"], published["commitId"])
        self.assertFalse(admin_item["archived"])

        # Student must be refused — the contract that protects the resource
        # list from leaking into the wrong role.
        status, _, body = self._request("GET", "/api/v1/playgrounds")
        self.assertEqual(status, 401)
        student_token = self._student_token(
            "wire-stu2-" + str(id(self)), "wire-stu2-pass"
        )
        status, _, body = self._request(
            "GET", "/api/v1/playgrounds", token=student_token
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))

    def test_course_assets_use_cookie_auth_and_runtime_overlay(self) -> None:
        """A same-origin iframe can read an authorized commit without a header."""

        admin_token = self._admin_token()
        playground_id = "wire-course"
        self._create_playground(admin_token, playground_id)
        manifest = json.dumps(
            {"schemaVersion": 1, "runtime": "python"}
        ).encode()
        published = self._publish(
            admin_token,
            playground_id,
            self._build_zip(
                [
                    ("picopg.json", manifest),
                    ("course/index.html", b"<h1>course</h1>"),
                ]
            ),
            "key-course",
        )
        self.server.runtime_path.joinpath("libs").mkdir(
            parents=True, exist_ok=True
        )
        self.server.runtime_path.joinpath("libs", "react.js").write_bytes(
            b"export {};"
        )

        cookie = {"Cookie": f"picopg_session={admin_token}"}
        base = f"/courses/{playground_id}/{published['commitId']}"
        status, headers, body = self._raw_request(
            "GET", base + "/course/index.html", headers=cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"<h1>course</h1>")
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("Content-Security-Policy", headers)

        status, _, body = self._raw_request(
            "GET", base + "/libs/react.js", headers=cookie
        )
        self.assertEqual((status, body), (200, b"export {};"))

        status, _, body = self._raw_request(
            "HEAD", base + "/course/index.html", headers=cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

        status, _, body = self._raw_request(
            "GET", base + "/course/index.html"
        )
        self.assertEqual((status, json.loads(body)["code"]), (401, "invalid_session"))

        status, _, body = self._raw_request(
            "GET", base + "/course/%2e%2e/picopg.json", headers=cookie
        )
        self.assertEqual((status, json.loads(body)["code"]), (404, "not_found"))

    def test_a_declared_cover_is_served_as_an_image(self) -> None:
        """A card cover is a plain `<img src>`, so its media type has to be the
        image's own; this Server sends `nosniff`, and an unlisted suffix would
        arrive as an opaque download."""

        admin_token = self._admin_token()
        playground_id = "wire-cover"
        self._create_playground(admin_token, playground_id)
        # A one-pixel PNG, the smallest thing a browser will decode.
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
        )
        published = self._publish(
            admin_token,
            playground_id,
            self._build_zip(
                [
                    (
                        "picopg.json",
                        json.dumps(
                            {
                                "schemaVersion": 1,
                                "runtime": "python",
                                "cover": "course/card.png",
                            }
                        ).encode(),
                    ),
                    ("course/index.html", b"<h1>c</h1>"),
                    ("course/card.png", png),
                ]
            ),
            "key-cover",
        )
        cookie = {"Cookie": f"picopg_session={admin_token}"}
        status, headers, body = self._raw_request(
            "GET",
            f"/courses/{playground_id}/{published['commitId']}/course/card.png",
            headers=cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(body, png)

    def test_course_csp_admits_the_pages_own_inline_script_and_wasm(self) -> None:
        """Pin the course policy's *content*, not only its presence.

        A published course is one HTML file that carries its own inline
        ``<script>``, and Pyodide compiles WASM under this same policy.  A
        stricter policy renders a blank page while every status code stays 200,
        so presence alone is not evidence: the two directives that make the
        page run have to be asserted, and they must match the desktop host's
        ``COURSE_CSP`` for the same artifact.
        """

        admin_token = self._admin_token()
        playground_id = "wire-csp"
        self._create_playground(admin_token, playground_id)
        published = self._publish(
            admin_token,
            playground_id,
            self._build_zip(
                [
                    ("picopg.json", json.dumps({"schemaVersion": 1, "runtime": "python"}).encode()),
                    ("course/index.html", b"<script>1</script>"),
                ]
            ),
            "key-csp",
        )
        cookie = {"Cookie": f"picopg_session={admin_token}"}
        status, headers, _ = self._raw_request(
            "GET",
            f"/courses/{playground_id}/{published['commitId']}/course/index.html",
            headers=cookie,
        )
        self.assertEqual(status, 200)
        csp = headers["Content-Security-Policy"]
        script_src = next(
            directive for directive in csp.split(";") if directive.strip().startswith("script-src")
        )
        self.assertIn("'unsafe-inline'", script_src, csp)
        self.assertIn("'wasm-unsafe-eval'", script_src, csp)
        self.assertIn("connect-src 'self'", csp)
        # A course page is never framed or given a token, so the policy must
        # not silently permit either.
        self.assertNotIn("frame-ancestors *", csp)

    def test_unpublished_playgrounds_are_invisible_in_resource_pool(self) -> None:
        """Only Playgrounds with a real published commit are listed in the
        teacher resource pool. An empty Playground (Admin created metadata
        but never uploaded a ZIP) must not leak as a phantom resource."""

        admin_token = self._admin_token()
        teacher_token = self._teacher_token()

        # Create one Playground, never publish anything to it.
        self._create_playground(admin_token, "wire-phantom")

        status, _, body = self._request(
            "GET", "/api/v1/playgrounds", token=teacher_token
        )
        self.assertEqual(status, 200)
        ids = {item["id"] for item in body["playgrounds"]}
        self.assertNotIn("wire-phantom", ids)

        # Publish into a second Playground, then list again — only the
        # published one appears.
        manifest = json.dumps({"schemaVersion": 1, "runtime": "python"}).encode()
        zip_body = self._build_zip(
            [("picopg.json", manifest), ("course/module/app.py", b"")]
        )
        self._create_playground(admin_token, "wire-real")
        self._publish(admin_token, "wire-real", zip_body, "key-real")

        status, _, body = self._request(
            "GET", "/api/v1/playgrounds", token=teacher_token
        )
        self.assertEqual(status, 200)
        ids = {item["id"] for item in body["playgrounds"]}
        self.assertIn("wire-real", ids)
        self.assertNotIn("wire-phantom", ids)

    def test_archived_playgrounds_drop_out_of_the_resource_pool(self) -> None:
        """Archiving excludes a Playground from the teacher list without
        removing the underlying bundle, so an archive-only state is never
        accidentally published."""

        playground_id = "wire-archive"
        manifest = json.dumps(
            {
                "schemaVersion": 1,
                "runtime": "python",
                "title": "将被归档",
                "summary": "归档后应从教师资源池消失",
                "requiresSubmission": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        zip_body = self._build_zip(
            [("picopg.json", manifest), ("course/module/app.py", b"")]
        )

        admin_token = self._admin_token()
        teacher_token = self._teacher_token()
        self._create_playground(admin_token, playground_id)
        self._publish(admin_token, playground_id, zip_body, "key-archive")

        status, _, body = self._request(
            "GET", "/api/v1/playgrounds", token=teacher_token
        )
        self.assertEqual(status, 200)
        ids_before = {item["id"] for item in body["playgrounds"]}
        self.assertIn(playground_id, ids_before)

        # Archive uses PATCH (not POST). The status code path on the wire is
        # the contract this test pins.
        status, _, _ = self._request(
            "PATCH",
            f"/api/v1/playgrounds/{urllib.parse.quote(playground_id, safe='')}",
            payload={"archived": True},
            token=admin_token,
        )
        self.assertEqual(status, 200)

        status, _, body = self._request(
            "GET", "/api/v1/playgrounds", token=teacher_token
        )
        self.assertEqual(status, 200)
        ids_after = {item["id"] for item in body["playgrounds"]}
        self.assertNotIn(playground_id, ids_after)

    def test_pagination_caps_page_size_and_returns_cursor(self) -> None:
        """Three published Playgrounds, ask for ``limit=1``; the response
        carries ``nextCursor`` and the second page covers the remaining items
        in stable id order."""

        admin_token = self._admin_token()
        teacher_token = self._teacher_token()
        manifests_data = {}
        for playground_id in ("wire-page-a", "wire-page-b", "wire-page-c"):
            self._create_playground(admin_token, playground_id)
            manifest = json.dumps(
                {
                    "schemaVersion": 1,
                    "runtime": "python",
                    "title": playground_id,
                }
            ).encode()
            zip_body = self._build_zip(
                [
                    ("picopg.json", manifest),
                    ("course/module/app.py", b""),
                ]
            )
            self._publish(
                admin_token, playground_id, zip_body, "key-" + playground_id
            )

        status, _, first = self._request(
            "GET", "/api/v1/playgrounds?limit=1", token=teacher_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(first["playgrounds"]), 1)
        self.assertIsNotNone(first["nextCursor"])

        status, _, second = self._request(
            "GET",
            f"/api/v1/playgrounds?limit=1&cursor={urllib.parse.quote(first['nextCursor'], safe='')}",
            token=teacher_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(second["playgrounds"]), 1)
        first_id = first["playgrounds"][0]["id"]
        second_id = second["playgrounds"][0]["id"]
        self.assertNotEqual(first_id, second_id)

        # Stable ordering: every id from the first page is strictly less than
        # every id from the second page, regardless of publication state.
        self.assertLess(first_id, second_id)

        # The third page is reachable too, and the final page carries no
        # cursor because there is nothing left.
        status, _, page3 = self._request(
            "GET",
            f"/api/v1/playgrounds?limit=1&cursor={urllib.parse.quote(second['nextCursor'], safe='')}",
            token=teacher_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(page3["playgrounds"]), 1)
        self.assertIsNone(page3["nextCursor"])

    def test_idempotent_retry_returns_stored_response(self) -> None:
        """A retry with the same Idempotency-Key returns the original response
        verbatim — proving the Client's idempotency-key contract is honored
        and no second commit is created."""

        playground_id = "wire-idempotent"
        manifest = json.dumps({"schemaVersion": 1, "runtime": "python"}).encode()
        zip_body = self._build_zip(
            [("picopg.json", manifest), ("course/module/app.py", b"x")]
        )

        admin_token = self._admin_token()
        self._create_playground(admin_token, playground_id)

        first = self._publish(admin_token, playground_id, zip_body, "key-1")
        second = self._publish(admin_token, playground_id, zip_body, "key-1")
        self.assertEqual(first, second)
        self.assertEqual(first["commitId"], second["commitId"])
        self.assertFalse(first["noOp"])

    def test_duplicate_bundle_with_fresh_key_is_a_noop(self) -> None:
        """Publishing the same bundle twice with two different
        Idempotency-Keys hits the no-tree-change short circuit: the second
        call returns ``noOp: True`` and the same commit id, so the resource
        pool never surfaces a phantom duplicate."""

        playground_id = "wire-noop"
        manifest = json.dumps({"schemaVersion": 1, "runtime": "python"}).encode()
        zip_body = self._build_zip(
            [("picopg.json", manifest), ("course/module/app.py", b"y")]
        )

        admin_token = self._admin_token()
        self._create_playground(admin_token, playground_id)

        first = self._publish(admin_token, playground_id, zip_body, "key-A")
        second = self._publish(admin_token, playground_id, zip_body, "key-B")
        self.assertFalse(first["noOp"])
        self.assertTrue(second["noOp"])
        self.assertEqual(first["commitId"], second["commitId"])


if __name__ == "__main__":
    unittest.main()
