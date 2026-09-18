"""HTTP tests for the two functions this service has.

Reads are anonymous: catalog, manifest and exact files.
Writes are the operator's bundle upload and a client's bounded submission.
Every test drives a real server over a real socket against a temporary data
directory, so a passing test is evidence about the HTTP contract rather than
about an in-process call.
"""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
import zipfile
from http.client import HTTPConnection
from pathlib import Path

# Allow ``import server`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import (  # noqa: E402
    MAX_SUBMISSION_JSON_BODY_BYTES,
    make_server,
)

OPERATOR_TOKEN = "test-operator-token-value"
MANIFEST = b'{"schemaVersion":1,"runtime":"python"}'


class ServerTestCase(unittest.TestCase):
    """One running server, one temporary data directory, one operator token."""

    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        base = Path(cls.temporary_directory.name)
        cls.database_path = base / "app.db"
        cls.server = make_server(
            host="127.0.0.1",
            port=0,
            database_path=cls.database_path,
            repository_path=base / "picopg.git",
            operator_token=OPERATOR_TOKEN,
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
        headers.update(extra_headers or {})

        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        status = response.status
        response_headers = {
            key.lower(): value for key, value in response.getheaders()
        }
        connection.close()

        decoded = None
        if response_body:
            try:
                decoded = json.loads(response_body)
            except json.JSONDecodeError:
                decoded = response_body
        return status, response_headers, decoded

    # ----- operator fixtures ---------------------------------------------

    def build_zip(self, entries=None):
        if entries is None:
            entries = (
                ("picopg.json", MANIFEST),
                ("course/module/app.py", b"print(1)"),
                ("starter/setup.py", b"setup"),
            )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return buffer.getvalue()

    def create_playground(self, playground_id):
        return self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": playground_id},
            token=OPERATOR_TOKEN,
        )

    def publish(self, playground_id, archive=None, key="key-1"):
        return self.request(
            "POST",
            f"/api/v1/playgrounds/{playground_id}/commits",
            raw_body=archive if archive is not None else self.build_zip(),
            token=OPERATOR_TOKEN,
            content_type="application/zip",
            extra_headers={"Idempotency-Key": key},
        )

    def seed(self, playground_id=None):
        """Create and publish one Playground; return ``(id, commitId)``."""

        playground_id = playground_id or f"pg-{uuid.uuid4().hex[:8]}"
        self.create_playground(playground_id)
        status, _, body = self.publish(playground_id)
        assert status == 201, (status, body)
        return playground_id, body["commitId"]

    def submit(self, playground_id, commit_id, **overrides):
        """Hand one piece of work in; overrides replace any request field."""

        payload = {
            "playgroundId": playground_id,
            "commitId": commit_id,
            "student": "2026-001",
            "files": {"work/main.py": "print(1)\n"},
        }
        payload.update(overrides)
        return self.request(
            "POST",
            "/api/v1/submissions",
            payload,
            extra_headers={"Idempotency-Key": str(uuid.uuid4())},
        )


class DistributionTest(ServerTestCase):
    """What a client can read: the catalog, a manifest and exact bytes."""

    def test_health_is_anonymous(self):
        status, _, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok\n")

    def test_catalog_lists_a_published_commit(self):
        playground_id, commit_id = self.seed()

        status, _, body = self.request("GET", "/api/v1/catalog")
        self.assertEqual(status, 200)
        self.assertIn(
            {"playgroundId": playground_id, "commitId": commit_id},
            body["catalog"],
        )

    def test_catalog_walks_every_page_without_repeating_an_entry(self):
        first, _ = self.seed("pg-page-a")
        second, _ = self.seed("pg-page-b")

        # The server is shared by this whole test class, so the walk has to
        # read the catalog as it really is rather than assume only two rows.
        ids = []
        cursor = None
        while True:
            query = "?limit=2" if cursor is None else f"?limit=2&cursor={cursor}"
            status, _, body = self.request("GET", f"/api/v1/catalog{query}")
            self.assertEqual(status, 200)
            self.assertLessEqual(len(body["catalog"]), 2)
            ids.extend(entry["playgroundId"] for entry in body["catalog"])
            cursor = body["nextCursor"]
            if cursor is None:
                break

        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(first, ids)
        self.assertIn(second, ids)

    def test_catalog_rejects_a_cursor_from_another_route(self):
        self.seed()
        _, _, catalog = self.request("GET", "/api/v1/catalog")

        status, _, body = self.request(
            "GET", f"/api/v1/catalog?cursor={catalog['nextCursor']}"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_request")

    def test_manifest_lists_the_published_files(self):
        playground_id, commit_id = self.seed()

        status, _, body = self.request(
            "GET",
            f"/api/v1/playgrounds/{playground_id}/commits/{commit_id}/manifest",
        )
        self.assertEqual(status, 200)
        manifest = body["manifest"]
        self.assertEqual(manifest["commitId"], commit_id)
        self.assertEqual(
            sorted(entry["path"] for entry in manifest["files"]),
            ["course/module/app.py", "picopg.json", "starter/setup.py"],
        )
        for entry in manifest["files"]:
            self.assertEqual(len(entry["sha256"]), 64)

    def test_commit_file_returns_exact_bytes(self):
        playground_id, commit_id = self.seed()

        status, headers, body = self.request(
            "GET",
            f"/api/v1/playgrounds/{playground_id}/commits/{commit_id}"
            "/files/course/module/app.py",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"print(1)")
        self.assertEqual(headers["content-type"], "application/octet-stream")
        self.assertEqual(
            headers["cache-control"], "public, max-age=31536000, immutable"
        )

    def test_manifest_of_an_unknown_commit_is_not_found(self):
        playground_id, _ = self.seed()

        status, _, body = self.request(
            "GET",
            f"/api/v1/playgrounds/{playground_id}/commits/{'0' * 40}/manifest",
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")

    def test_commit_file_outside_the_tree_is_not_found(self):
        playground_id, commit_id = self.seed()

        status, _, body = self.request(
            "GET",
            f"/api/v1/playgrounds/{playground_id}/commits/{commit_id}"
            "/files/course/../picopg.json",
        )
        self.assertEqual(status, 404)


class PublishTest(ServerTestCase):
    """What the operator can write, and what a missing token gets."""

    def test_publishing_requires_the_operator_token(self):
        playground_id = f"pg-{uuid.uuid4().hex[:8]}"

        status, _, body = self.request(
            "POST", "/api/v1/playgrounds", {"id": playground_id}
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "invalid_credentials")

        wrong = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": playground_id},
            token="not-the-token",
        )
        self.assertEqual(wrong[0], 401)

    def test_creating_the_same_playground_twice_conflicts(self):
        playground_id, _ = self.seed()

        status, _, body = self.create_playground(playground_id)
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "conflict")

    def test_publishing_an_invalid_identifier_is_rejected(self):
        status, _, body = self.request(
            "POST", "/api/v1/playgrounds", {"id": "not a valid id"},
            token=OPERATOR_TOKEN,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_request")

    def test_publishing_an_incomplete_bundle_is_rejected(self):
        playground_id = f"pg-{uuid.uuid4().hex[:8]}"
        self.create_playground(playground_id)

        status, _, body = self.publish(
            playground_id, self.build_zip([("course/module/app.py", b"print(1)")])
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_bundle")

    def test_republishing_the_same_bundle_is_a_no_op(self):
        playground_id, commit_id = self.seed()

        status, _, body = self.publish(playground_id, key="key-2")
        self.assertEqual(status, 201)
        self.assertEqual(body["commitId"], commit_id)
        self.assertTrue(body["noOp"])

    def test_retrying_with_the_same_key_replays_the_result(self):
        playground_id = f"pg-{uuid.uuid4().hex[:8]}"
        self.create_playground(playground_id)

        first = self.publish(playground_id, key="retry-key")
        second = self.publish(playground_id, key="retry-key")
        self.assertEqual(first[0], 201)
        self.assertEqual(second[0], 201)
        self.assertEqual(first[2], second[2])

    def test_reusing_a_key_for_another_body_conflicts(self):
        playground_id = f"pg-{uuid.uuid4().hex[:8]}"
        self.create_playground(playground_id)

        self.publish(playground_id, key="shared-key")
        status, _, body = self.publish(
            playground_id,
            self.build_zip(
                [
                    ("picopg.json", MANIFEST),
                    ("course/module/app.py", b"print(2)"),
                ]
            ),
            key="shared-key",
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "idempotency_conflict")

    def test_publishing_needs_an_idempotency_key(self):
        playground_id = f"pg-{uuid.uuid4().hex[:8]}"
        self.create_playground(playground_id)

        status, _, body = self.request(
            "POST",
            f"/api/v1/playgrounds/{playground_id}/commits",
            raw_body=self.build_zip(),
            token=OPERATOR_TOKEN,
            content_type="application/zip",
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_request")

    def test_uploading_to_an_unknown_playground_is_not_found(self):
        status, _, body = self.publish(f"pg-{uuid.uuid4().hex[:8]}")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")


class RetiringTest(ServerTestCase):
    """Operator takes a course out of the catalog: students stop seeing it.

    Retiring is not a delete: the row, the submissions that reference it and the
    commits all stay, so the same id can be published again.
    """

    def catalog_ids(self):
        status, _, body = self.request("GET", "/api/v1/catalog")
        self.assertEqual(status, 200)
        return [entry["playgroundId"] for entry in body["catalog"]]

    def retire(self, playground_id, token=OPERATOR_TOKEN):
        return self.request(
            "DELETE", f"/api/v1/playgrounds/{playground_id}", token=token
        )

    def test_retiring_a_playground_takes_it_out_of_the_catalog(self):
        playground_id, commit_id = self.seed("pg-retire")
        self.assertIn("pg-retire", self.catalog_ids())

        status, _, body = self.retire("pg-retire")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"playgroundId": "pg-retire", "retired": True})

        self.assertNotIn("pg-retire", self.catalog_ids())
        # Nothing is readable any more: the ref is what every read resolves first.
        self.assertEqual(
            self.request(
                "GET",
                f"/api/v1/playgrounds/pg-retire/commits/{commit_id}/manifest",
            )[0],
            404,
        )

        # The record is untouched: publishing the same id brings the course back
        # and readable. The restored commit is a new one — the old ref is gone for
        # good, so any client pinned to the retired commit would 404.
        status, _, body = self.publish("pg-retire")
        self.assertEqual(status, 201)
        self.assertIn("pg-retire", self.catalog_ids())
        restored = self.request(
            "GET",
            f"/api/v1/playgrounds/pg-retire/commits/{body['commitId']}/manifest",
        )
        self.assertEqual(restored[0], 200)

    def test_a_publish_replayed_after_a_retire_publishes_again(self):
        playground_id, _ = self.seed("pg-retire")
        self.assertEqual(self.retire(playground_id)[0], 200)

        # The very same idempotency key as the first publish. The retire dropped
        # the recorded result, so this is a real publish: replaying it would
        # answer 201 with a commit that no longer exists.
        status, _, body = self.publish(playground_id, key="key-1")
        self.assertEqual(status, 201)
        self.assertIn(playground_id, self.catalog_ids())
        manifest = self.request(
            "GET",
            f"/api/v1/playgrounds/{playground_id}/commits/{body['commitId']}/manifest",
        )
        self.assertEqual(manifest[0], 200)

    def test_retiring_needs_the_operator_token(self):
        self.seed("pg-retire")

        self.assertEqual(self.retire("pg-retire", token=None)[0], 401)
        self.assertEqual(self.retire("pg-retire", token="wrong-token-000000")[0], 401)
        self.assertIn("pg-retire", self.catalog_ids())

    def test_retiring_an_unknown_playground_is_not_found(self):
        self.assertEqual(self.retire("pg-never-published")[0], 404)

    def test_retiring_twice_reports_that_it_was_already_gone(self):
        self.seed("pg-retire")

        self.assertEqual(self.retire("pg-retire")[2]["retired"], True)
        status, _, body = self.retire("pg-retire")
        self.assertEqual(status, 200)
        self.assertEqual(body["retired"], False)

    def test_a_retired_course_keeps_the_work_already_handed_in(self):
        playground_id, commit_id = self.seed("pg-retire")
        submission_id = self.submit(playground_id, commit_id)[2]["submission"]["id"]

        self.assertEqual(self.retire(playground_id)[0], 200)

        # The submission is still readable, and its Playground still exists for it.
        status, _, body = self.request(
            "GET", f"/api/v1/submissions/{submission_id}", token=OPERATOR_TOKEN
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["submission"]["playgroundId"], playground_id)

        # A new submission against a retired course is refused.
        self.assertEqual(self.submit(playground_id, commit_id)[0], 404)


class SubmissionTest(ServerTestCase):
    """The one anonymous write: a bounded file set from a client."""

    def test_a_client_can_submit_without_an_account(self):
        playground_id, commit_id = self.seed()

        status, _, body = self.submit(playground_id, commit_id)
        self.assertEqual(status, 201)
        submission = body["submission"]
        self.assertEqual(submission["playgroundId"], playground_id)
        self.assertEqual(submission["commitId"], commit_id)
        self.assertEqual(submission["student"], "2026-001")

    def test_a_submission_is_filed_under_its_class_and_name(self):
        playground_id, commit_id = self.seed()

        status, _, body = self.submit(playground_id, commit_id, student="张三（1 班）")
        self.assertEqual(status, 201)
        submission_id = body["submission"]["id"]

        # One directory per piece of work: <班级>/<姓名>/<playgroundId>/work/main.py
        class_directory = Path(self.server.submissions_path) / "1 班" / "张三"
        self.assertTrue(class_directory.is_dir())
        (folder,) = list(class_directory.iterdir())
        self.assertEqual(folder.name, playground_id)
        self.assertEqual(
            (folder / "work/main.py").read_text(encoding="utf-8"), "print(1)\n"
        )

        # The folder says whose work it is without anyone having to ask the API.
        manifest = json.loads((folder / "submission.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], submission_id)
        self.assertEqual(manifest["student"], "张三（1 班）")
        self.assertEqual(manifest["playgroundId"], playground_id)
        self.assertEqual(manifest["commitId"], commit_id)
        self.assertEqual(manifest["files"], ["work/main.py"])
        self.assertEqual(manifest["ordinal"], 1)

    def test_a_hostile_student_name_stays_inside_the_submissions_root(self):
        playground_id, commit_id = self.seed()
        root = Path(self.server.submissions_path)

        status, _, _ = self.submit(
            playground_id, commit_id, student="../../etc/passwd（../../..）"
        )
        self.assertEqual(status, 201)

        # Every directory the request created is under the injected root, and
        # every segment is a single name: no separators, no `..`.
        created = list(root.rglob("*"))
        self.assertTrue(created)
        for path in created:
            self.assertIn(root, path.parents)
            self.assertNotIn("..", path.relative_to(root).parts)
        self.assertFalse((root.parent.parent / "etc").exists())

    def test_handing_the_same_course_in_twice_numbers_the_folder(self):
        playground_id, commit_id = self.seed()

        # A student of their own: the class shares one database, so a name another
        # test already used would start this one at a later ordinal.
        first = self.submit(playground_id, commit_id, student="王五（7 班）")
        second = self.submit(
            playground_id,
            commit_id,
            student="王五（7 班）",
            files={"work/main.py": "print(2)\n"},
        )
        self.assertEqual((first[0], second[0]), (201, 201))

        student_directory = Path(self.server.submissions_path) / "7 班" / "王五"
        self.assertEqual(
            sorted(entry.name for entry in student_directory.iterdir()),
            [playground_id, f"{playground_id}-2"],
        )
        # Each hand-in keeps its own work; the first one is not overwritten.
        self.assertEqual(
            (student_directory / playground_id / "work/main.py").read_text(encoding="utf-8"),
            "print(1)\n",
        )
        self.assertEqual(
            (student_directory / f"{playground_id}-2" / "work/main.py").read_text(encoding="utf-8"),
            "print(2)\n",
        )

    def test_an_exact_retry_rewrites_the_same_folder(self):
        playground_id, commit_id = self.seed()
        key = str(uuid.uuid4())
        payload = {
            "playgroundId": playground_id,
            "commitId": commit_id,
            "student": "李四（2 班）",
            "files": {"work/main.py": "print(2)\n"},
        }

        first = self.request("POST", "/api/v1/submissions", payload, extra_headers={"Idempotency-Key": key})
        second = self.request("POST", "/api/v1/submissions", payload, extra_headers={"Idempotency-Key": key})
        self.assertEqual((first[0], second[0]), (201, 201))
        self.assertEqual(first[2], second[2])

        # The replay wrote the same bytes to the same place, so there is still
        # exactly one folder for that piece of work.
        (folder,) = list((Path(self.server.submissions_path) / "2 班" / "李四").iterdir())
        self.assertEqual((folder / "work/main.py").read_text(encoding="utf-8"), "print(2)\n")

    def test_the_operator_reads_the_submitted_files_back(self):
        playground_id, commit_id = self.seed()
        submission_id = self.submit(playground_id, commit_id)[2]["submission"]["id"]

        status, _, body = self.request(
            "GET",
            f"/api/v1/submissions/{submission_id}",
            token=OPERATOR_TOKEN,
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            body["submission"]["files"],
            [{"path": "work/main.py", "text": "print(1)\n"}],
        )

    def test_listing_submissions_requires_the_operator_token(self):
        playground_id, commit_id = self.seed()
        self.submit(playground_id, commit_id)

        anonymous = self.request("GET", "/api/v1/submissions")
        self.assertEqual(anonymous[0], 401)

        listed = self.request(
            "GET", "/api/v1/submissions", token=OPERATOR_TOKEN
        )
        self.assertEqual(listed[0], 200)
        self.assertGreaterEqual(len(listed[2]["submissions"]), 1)
        for entry in listed[2]["submissions"]:
            self.assertNotIn("files", entry)

    def test_a_submission_to_an_unpublished_commit_is_not_found(self):
        playground_id, _ = self.seed()

        status, _, body = self.submit(playground_id, "0" * 40)
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")

    def test_a_submission_outside_work_is_rejected(self):
        playground_id, commit_id = self.seed()

        status, _, body = self.submit(
            playground_id, commit_id, files={"course/index.html": "<p>"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_request")

    def test_a_submission_with_a_malformed_student_is_rejected(self):
        playground_id, commit_id = self.seed()

        blank = self.submit(playground_id, commit_id, student="   ")
        self.assertEqual(blank[0], 400)

        long_name = self.submit(playground_id, commit_id, student="a" * 129)
        self.assertEqual(long_name[0], 400)

    def test_a_submission_may_hand_in_nothing(self):
        playground_id, commit_id = self.seed()

        status, _, body = self.submit(playground_id, commit_id, files={})
        self.assertEqual(status, 201)
        self.assertEqual(body["submission"]["student"], "2026-001")

    def test_retrying_a_submission_returns_the_same_record(self):
        playground_id, commit_id = self.seed()
        payload = {
            "playgroundId": playground_id,
            "commitId": commit_id,
            "student": "2026-002",
            "files": {"work/main.py": "print(2)\n"},
        }

        first = self.request(
            "POST",
            "/api/v1/submissions",
            payload,
            extra_headers={"Idempotency-Key": "same-key"},
        )
        second = self.request(
            "POST",
            "/api/v1/submissions",
            payload,
            extra_headers={"Idempotency-Key": "same-key"},
        )
        self.assertEqual(first[0], 201)
        self.assertEqual(first[2], second[2])

    def test_two_students_may_use_the_same_idempotency_key(self):
        playground_id, commit_id = self.seed()

        first = self.submit(
            playground_id, commit_id, student="2026-003", files={"work/a.py": "1"}
        )
        second = self.submit(
            playground_id, commit_id, student="2026-004", files={"work/a.py": "1"}
        )
        self.assertEqual(first[0], 201)
        self.assertEqual(second[0], 201)
        self.assertNotEqual(
            first[2]["submission"]["id"], second[2]["submission"]["id"]
        )

    def test_a_non_json_body_is_rejected(self):
        status, _, body = self.request(
            "POST",
            "/api/v1/submissions",
            raw_body=b"{}",
            content_type="text/plain",
            extra_headers={"Idempotency-Key": "k"},
        )
        self.assertEqual(status, 415)
        self.assertEqual(body["code"], "unsupported_media_type")

    def test_an_oversized_body_is_refused_before_it_is_parsed(self):
        try:
            status, _, body = self.request(
                "POST",
                "/api/v1/submissions",
                raw_body=b"x" * (MAX_SUBMISSION_JSON_BODY_BYTES + 1),
                content_type="application/json",
                extra_headers={"Idempotency-Key": "k"},
            )
        except OSError:
            # The Content-Length bound is checked before the body is read, so
            # a client still writing may see the closed connection (a broken
            # pipe or a reset) instead of the 413.  Both are the same refusal.
            return
        self.assertEqual(status, 413)
        self.assertEqual(body["code"], "request_too_large")


class RoutingTest(ServerTestCase):
    """What an unknown route, an unsupported verb, and a bad payload get."""

    def test_an_unknown_api_path_is_not_found(self):
        status, _, body = self.request("GET", "/api/v1/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")

    def test_an_unsupported_verb_on_a_known_path_lists_the_methods(self):
        status, headers, body = self.request("DELETE", "/api/v1/catalog")
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "GET, HEAD")
        self.assertEqual(body["code"], "method_not_allowed")

    def test_a_head_request_has_no_body(self):
        self.seed()

        status, headers, body = self.request("HEAD", "/api/v1/catalog")
        self.assertEqual(status, 200)
        self.assertIsNone(body)
        self.assertGreater(int(headers["content-length"]), 0)


class OperatorTokenTest(unittest.TestCase):
    """The server refuses to start without a usable operator token."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.base = Path(self.temporary_directory.name)

    def make(self, token):
        previous = os.environ.get("PICOPG_OPERATOR_TOKEN")

        def restore():
            if previous is None:
                os.environ.pop("PICOPG_OPERATOR_TOKEN", None)
            else:
                os.environ["PICOPG_OPERATOR_TOKEN"] = previous

        self.addCleanup(restore)
        server_instance = make_server(
            host="127.0.0.1",
            port=0,
            database_path=self.base / "app.db",
            repository_path=self.base / "picopg.git",
            operator_token=token,
        )
        server_instance.server_close()
        return server_instance

    def test_a_short_token_refuses_to_start(self):
        with self.assertRaises(ValueError):
            self.make("short")

    def test_the_environment_variable_is_the_default_source(self):
        os.environ["PICOPG_OPERATOR_TOKEN"] = "environment-operator-token"
        instance = self.make(None)
        self.assertEqual(
            instance.operator_token, "environment-operator-token"
        )

    def test_a_missing_token_refuses_to_start(self):
        with self.assertRaises(ValueError):
            self.make(None)


if __name__ == "__main__":
    unittest.main()
