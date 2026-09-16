"""End-to-end invariant tests over the public HTTP API.

Every test drives a complete flow against one ephemeral server and then
inspects Git or SQLite only to assert the resulting postcondition.  The tests
pin the documented security and history invariants:

1.  public registration can never gain a privileged role;
2.  a known private commit stays unreadable without a Release;
3.  a Release stays pinned after the Playground HEAD advances;
4.  a Work stays pinned to its initial Playground version;
5.  transferring a Class preserves existing Submission/Comment records;
6.  a Teacher cannot read or comment on another Class's Submission;
7.  Admin metadata access is never Work or feedback body access;
8.  stale Work sync/restore never rewinds a ref or creates a merge commit;
9.  repeated creates are immutable and conflicting reuse is rejected;
10. deleting and rebuilding the generated cache changes no authoritative ID.
"""

import base64
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import uuid
import zipfile
from http.client import HTTPConnection
from pathlib import Path

import accounts
import generated
import manifests
import repository
from database import open_database
from server import make_server


class EndToEndInvariantTest(unittest.TestCase):
    """One ephemeral server for every invariant flow."""

    MANIFEST = b'{"schemaVersion":1,"runtime":"python"}'

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

    # ----- generic HTTP plumbing -----------------------------------------

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
        if extra_headers:
            headers.update(extra_headers)

        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
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

    def raw(self, method, path, token=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        status = response.status
        response_headers = {
            key.lower(): value for key, value in response.getheaders()
        }
        connection.close()
        return status, response_headers, body

    def unique(self, prefix):
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    # ----- accounts ------------------------------------------------------

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
        self.assertEqual(status, 200, body)
        return body["sessionToken"]

    # ----- SQLite postconditions -----------------------------------------

    def db_one(self, query, params=()):
        connection = open_database(self.database_path)
        try:
            row = connection.execute(query, params).fetchone()
            return None if row is None else dict(row)
        finally:
            connection.close()

    def db_scalar(self, query, params=()):
        connection = open_database(self.database_path)
        try:
            row = connection.execute(query, params).fetchone()
            return None if row is None else row[0]
        finally:
            connection.close()

    # ----- Git postconditions --------------------------------------------

    def git(self, *arguments):
        completed = subprocess.run(
            ["git", "-C", str(self.repository_path), *arguments],
            capture_output=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", "replace"),
        )
        return completed.stdout

    def commit_parents(self, commit_id):
        fields = (
            self.git("rev-list", "--parents", "-n", "1", commit_id)
            .decode("ascii")
            .split()
        )
        return fields[1:]

    def parent_counts(self, tip):
        counts = {}
        for line in self.git("rev-list", "--parents", tip).decode().splitlines():
            fields = line.split()
            counts[fields[0]] = len(fields) - 1
        return counts

    def tree_of(self, commit_id):
        return (
            self.git("rev-parse", f"{commit_id}^{{tree}}").decode().strip()
        )

    def is_ancestor(self, ancestor, descendant):
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(self.repository_path),
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            ],
            capture_output=True,
        )
        return completed.returncode == 0

    def work_head(self, playground_id, student_id):
        return repository.resolve_work_head(
            self.server.repository, playground_id, student_id
        )

    # ----- playground / bundle helpers -----------------------------------

    def create_playground(self, token, playground_id):
        status, _, body = self.request(
            "POST",
            "/api/v1/playgrounds",
            {"id": playground_id},
            token=token,
        )
        self.assertEqual(status, 201, body)
        return body["playground"]

    def set_policy(self, token, playground_id, policy):
        status, _, body = self.request(
            "PATCH",
            f"/api/v1/playgrounds/{playground_id}",
            {"policy": policy},
            token=token,
        )
        self.assertEqual(status, 200, body)
        return body["playground"]

    @staticmethod
    def default_entries():
        return (
            ("picopg.json", EndToEndInvariantTest.MANIFEST),
            ("course/module/app.py", b"print(1)"),
        )

    def build_zip(self, entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return buffer.getvalue()

    def publish_raw(self, token, playground_id, entries=None, key=None):
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

    def publish(self, token, playground_id, entries=None, key=None):
        status, _, body = self.publish_raw(
            token, playground_id, entries, key
        )
        self.assertEqual(status, 201, body)
        return body

    def manifest(self, token, playground_id, commit_id):
        return self.request(
            "GET",
            f"/api/v1/playgrounds/{playground_id}"
            f"/commits/{commit_id}/manifest",
            token=token,
        )

    def commit_file(self, token, playground_id, commit_id, path):
        status, headers, body = self.raw(
            "GET",
            f"/api/v1/playgrounds/{playground_id}"
            f"/commits/{commit_id}/files/{path}",
            token=token,
        )
        if headers.get("content-type", "").startswith("application/json"):
            return status, headers, json.loads(body)
        return status, headers, body

    # ----- class / release helpers ---------------------------------------

    def create_class(self, admin_token, teacher_id):
        status, _, body = self.request(
            "POST",
            "/api/v1/admin/classes",
            {"name": self.unique("Class"), "teacherId": teacher_id},
            token=admin_token,
        )
        self.assertEqual(status, 201, body)
        return body["class"]

    def assign_student(self, admin_token, class_id, student_id):
        status, _, body = self.request(
            "PUT",
            f"/api/v1/admin/classes/{class_id}/members/{student_id}",
            token=admin_token,
        )
        self.assertEqual(status, 200, body)

    def change_owner(self, admin_token, class_id, teacher_id):
        return self.request(
            "PATCH",
            f"/api/v1/admin/classes/{class_id}/owner",
            {"teacherId": teacher_id},
            token=admin_token,
        )

    @staticmethod
    def release_payload(
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

    def post_release(self, token, class_id, payload, key=None):
        extra = {"Idempotency-Key": key or self.unique("release")}
        return self.request(
            "POST",
            f"/api/v1/classes/{class_id}/releases",
            payload,
            token=token,
            extra_headers=extra,
        )

    def release(self, token, class_id, payload, key=None):
        status, _, body = self.post_release(token, class_id, payload, key)
        self.assertEqual(status, 201, body)
        return body["release"]

    # ----- Work / Submission / Comment helpers ---------------------------

    @staticmethod
    def files(mapping):
        return [
            {"path": path, "data": base64.b64encode(data).decode("ascii")}
            for path, data in mapping.items()
        ]

    def sync_raw(
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

    def sync(self, token, playground_id, commit_id, mapping, base=None):
        status, _, body = self.sync_raw(
            token, playground_id, commit_id, mapping, base
        )
        self.assertIn(status, (200, 201), body)
        return body["workCommitId"]

    def submit_raw(self, token, release_id, work_commit_id, key):
        return self.request(
            "POST",
            f"/api/v1/releases/{release_id}/submissions",
            {"workCommitId": work_commit_id},
            token=token,
            extra_headers={"Idempotency-Key": key},
        )

    def submit(self, token, release_id, work_commit_id, key):
        status, _, body = self.submit_raw(
            token, release_id, work_commit_id, key
        )
        self.assertEqual(status, 201, body)
        return body["submission"]

    def comment_raw(self, token, submission_id, body, key=None):
        extra = {"Idempotency-Key": key or self.unique("comment")}
        return self.request(
            "POST",
            f"/api/v1/submissions/{submission_id}/comments",
            {"body": body},
            token=token,
            extra_headers=extra,
        )

    def comment(self, token, submission_id, body, key=None):
        status, _, payload = self.comment_raw(
            token, submission_id, body, key
        )
        self.assertEqual(status, 201, payload)
        return payload["comment"]

    def get_comments(self, token, submission_id):
        return self.request(
            "GET",
            f"/api/v1/submissions/{submission_id}/comments",
            token=token,
        )

    def get_submission(self, token, submission_id):
        return self.request(
            "GET", f"/api/v1/submissions/{submission_id}", token=token
        )

    def work_manifest_pin(self, work_commit_id):
        entries = repository.list_commit_tree(
            self.server.repository, work_commit_id
        )
        matches = [item for item in entries if item.path == ".picopg/work.json"]
        self.assertEqual(len(matches), 1)
        blobs = repository.read_blobs(
            self.server.repository, [matches[0].object_id]
        )
        return json.loads(blobs[matches[0].object_id].decode("utf-8"))[
            "playgroundCommitId"
        ]

    def new_course(self, submit=False):
        """Return an Admin/Teacher/Student course with an open Release."""

        admin = self.seed_user("admin")
        teacher = self.seed_user("teacher")
        student = self.seed_user("student")
        admin_token = self.login_token(admin.username)
        teacher_token = self.login_token(teacher.username)
        student_token = self.login_token(student.username)

        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        commit_id = self.publish(admin_token, playground_id)["commitId"]
        klass = self.create_class(admin_token, teacher.id)
        self.assign_student(admin_token, klass["id"], student.id)
        release = self.release(
            teacher_token,
            klass["id"],
            self.release_payload(playground_id, commit_id),
        )
        work_commit_id = self.sync(
            student_token,
            playground_id,
            commit_id,
            {"work/main.py": b"print('work')"},
        )

        result = {
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
        if submit:
            result["submission"] = self.submit(
                student_token,
                release["id"],
                work_commit_id,
                self.unique("submit"),
            )
        return result

    # =====================================================================
    # 1. Public registration cannot gain a privileged role.
    # =====================================================================

    def test_registration_never_grants_teacher_or_admin(self):
        for requested_role in ("admin", "teacher"):
            with self.subTest(requested_role=requested_role):
                username = self.unique("register")
                status, _, body = self.request(
                    "POST",
                    "/api/v1/register",
                    {
                        "username": username,
                        "password": "secret",
                        "role": requested_role,
                    },
                )
                self.assertEqual(status, 201)
                self.assertEqual(body["user"]["role"], "student")

                token = self.login_token(username)
                status, _, me = self.request(
                    "GET", "/api/v1/me", token=token
                )
                self.assertEqual(status, 200)
                self.assertEqual(me["user"]["role"], "student")

                # A privileged mutation is denied even though the account
                # asked for (and must not have received) Teacher/Admin.
                status, _, denied = self.request(
                    "POST",
                    "/api/v1/admin/users",
                    {
                        "username": self.unique("extra"),
                        "password": "secret",
                        "role": "teacher",
                    },
                    token=token,
                )
                self.assertEqual((status, denied["code"]), (403, "forbidden"))

                status, _, denied = self.request(
                    "POST",
                    "/api/v1/playgrounds",
                    {"id": self.unique("pg")},
                    token=token,
                )
                self.assertEqual((status, denied["code"]), (403, "forbidden"))

                stored = self.db_one(
                    "SELECT role FROM users WHERE username = ?",
                    (username,),
                )
                self.assertEqual(stored["role"], "student")

    # =====================================================================
    # 2. A known private commit stays unreadable without a Release.
    # =====================================================================

    def test_known_private_commit_is_unreadable_without_release(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)
        outsider = self.seed_user("student")
        outsider_token = self.login_token(outsider.username)
        teacher = self.seed_user("teacher")
        teacher_token = self.login_token(teacher.username)

        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        commit_id = self.publish(admin_token, playground_id)["commitId"]

        # The commit is real (a Git authority), and no Release grants it.
        self.assertEqual(
            repository.resolve_commit(self.server.repository, commit_id),
            commit_id,
        )
        self.assertEqual(
            self.db_scalar(
                "SELECT COUNT(*) FROM releases WHERE playground_id = ?",
                (playground_id,),
            ),
            0,
        )

        for label, token in (
            ("anonymous", None),
            ("outsider", outsider_token),
            ("unrelated-teacher", teacher_token),
        ):
            with self.subTest(actor=label):
                status, _, body = self.manifest(
                    token, playground_id, commit_id
                )
                self.assertEqual((status, body["code"]), (404, "not_found"))
                status, _, body = self.commit_file(
                    token, playground_id, commit_id, "picopg.json"
                )
                self.assertEqual((status, body["code"]), (404, "not_found"))

    # =====================================================================
    # 3. A Release stays pinned after the Playground HEAD advances.
    # =====================================================================

    def test_release_stays_pinned_after_head_advances(self):
        ctx = self.new_course()
        student_token = ctx["student_token"]
        release_id = ctx["release"]["id"]
        first_commit = ctx["commit_id"]

        second_commit = self.publish(
            ctx["admin_token"],
            ctx["playground_id"],
            (
                ("picopg.json", self.MANIFEST),
                ("course/module/app.py", b"print(2)"),
            ),
        )["commitId"]
        self.assertNotEqual(first_commit, second_commit)

        # Authoritative HEAD advanced ...
        self.assertEqual(
            manifests.current_playground_commit(
                self.server.repository, ctx["playground_id"]
            ),
            second_commit,
        )
        # ... but the Release row still pins the first commit.
        stored = self.db_one(
            "SELECT playground_commit_id FROM releases WHERE id = ?",
            (release_id,),
        )
        self.assertEqual(stored["playground_commit_id"], first_commit)

        status, _, body = self.request(
            "GET", f"/api/v1/releases/{release_id}", token=student_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["release"]["playgroundCommitId"], first_commit)

        status, _, _ = self.manifest(
            student_token, ctx["playground_id"], first_commit
        )
        self.assertEqual(status, 200)
        status, _, body = self.manifest(
            student_token, ctx["playground_id"], second_commit
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

        status, _, catalog = self.request(
            "GET", "/api/v1/catalog", token=student_token
        )
        self.assertEqual(status, 200)
        keys = {
            (item["playgroundId"], item["commitId"])
            for item in catalog["catalog"]
        }
        self.assertIn((ctx["playground_id"], first_commit), keys)
        self.assertNotIn((ctx["playground_id"], second_commit), keys)

    # =====================================================================
    # 4. A Work stays pinned to its initial Playground version.
    # =====================================================================

    def test_work_stays_pinned_to_initial_playground_version(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)
        student = self.seed_user("student")
        student_token = self.login_token(student.username)

        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        self.set_policy(admin_token, playground_id, "public")
        first_commit = self.publish(
            admin_token, playground_id
        )["commitId"]
        work_head = self.sync(
            student_token,
            playground_id,
            first_commit,
            {"work/main.py": b"print('initial')"},
        )

        # The Server-owned manifest records the initial pin.
        self.assertEqual(
            self.work_manifest_pin(work_head), first_commit
        )

        second_commit = self.publish(
            admin_token,
            playground_id,
            (
                ("picopg.json", self.MANIFEST),
                ("course/module/app.py", b"print(2)"),
            ),
        )["commitId"]
        self.assertNotEqual(first_commit, second_commit)

        # Appending against the newer HEAD is rejected; the pin wins.
        status, _, body = self.sync_raw(
            student_token,
            playground_id,
            second_commit,
            {"work/main.py": b"print('moved')"},
            base=work_head,
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            body["details"]["reason"], "work_playground_mismatch"
        )
        self.assertEqual(
            self.work_head(playground_id, student.id), work_head
        )
        self.assertEqual(self.work_manifest_pin(work_head), first_commit)

    # =====================================================================
    # 5. Transfer changes future visibility but not existing records.
    # =====================================================================

    def test_transfer_preserves_existing_submission_and_comments(self):
        ctx = self.new_course(submit=True)
        submission_id = ctx["submission"]["id"]
        first_comment = self.comment(
            ctx["teacher_token"], submission_id, "from first teacher"
        )

        submission_before = self.db_one(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        )
        comment_before = self.db_one(
            "SELECT * FROM comments WHERE id = ?", (first_comment["id"],)
        )
        self.assertIsNotNone(submission_before)
        self.assertIsNotNone(comment_before)

        # Before transfer, the owning Teacher sees the Release.
        status, _, _ = self.request(
            "GET",
            f"/api/v1/releases/{ctx['release']['id']}",
            token=ctx["teacher_token"],
        )
        self.assertEqual(status, 200)

        new_teacher = self.seed_user("teacher")
        status, _, body = self.change_owner(
            ctx["admin_token"], ctx["class"]["id"], new_teacher.id
        )
        self.assertEqual(status, 200, body)
        new_token = self.login_token(new_teacher.username)

        # Future visibility follows ownership in both directions.
        status, _, body = self.request(
            "GET",
            f"/api/v1/releases/{ctx['release']['id']}",
            token=ctx["teacher_token"],
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))
        status, _, body = self.request(
            "GET",
            f"/api/v1/releases/{ctx['release']['id']}",
            token=new_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["release"]["id"], ctx["release"]["id"])

        # No stored Submission or Comment row was rewritten by the transfer.
        self.assertEqual(
            self.db_one(
                "SELECT * FROM submissions WHERE id = ?", (submission_id,)
            ),
            submission_before,
        )
        self.assertEqual(
            self.db_one(
                "SELECT * FROM comments WHERE id = ?",
                (first_comment["id"],),
            ),
            comment_before,
        )

        # The Student still sees the exact original records.
        status, _, body = self.get_submission(
            ctx["student_token"], submission_id
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["submission"], ctx["submission"])
        status, _, listed = self.get_comments(
            ctx["student_token"], submission_id
        )
        self.assertEqual(status, 200)
        self.assertEqual(listed["comments"], [first_comment])

        # The new Teacher reads the old comment and appends to the chain.
        status, _, listed = self.get_comments(new_token, submission_id)
        self.assertEqual(status, 200)
        self.assertEqual(listed["comments"], [first_comment])
        appended = self.comment(new_token, submission_id, "from new teacher")
        self.assertEqual(appended["authorUserId"], new_teacher.id)
        self.assertEqual(
            self.db_scalar(
                "SELECT COUNT(*) FROM comments WHERE submission_id = ?",
                (submission_id,),
            ),
            2,
        )

    # =====================================================================
    # 6. A Teacher cannot read or comment on another Class's Submission.
    # =====================================================================

    def test_teacher_is_scoped_to_owned_class_submissions(self):
        first = self.new_course(submit=True)
        second = self.new_course(submit=True)

        # The owning Teacher has full access to their own Class.
        status, _, _ = self.get_submission(
            first["teacher_token"], first["submission"]["id"]
        )
        self.assertEqual(status, 200)

        foreign_teacher = first["teacher_token"]
        foreign_submission = second["submission"]["id"]

        status, _, body = self.get_submission(
            foreign_teacher, foreign_submission
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

        status, _, body = self.get_comments(
            foreign_teacher, foreign_submission
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

        status, _, body = self.request(
            "GET",
            f"/api/v1/releases/{second['release']['id']}/submissions",
            token=foreign_teacher,
        )
        self.assertEqual((status, body["code"]), (404, "not_found"))

        status, _, body = self.comment_raw(
            foreign_teacher, foreign_submission, "intruding", self.unique("k")
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))

        # The failed attempts wrote nothing to the other Class's records.
        self.assertEqual(
            self.db_scalar(
                "SELECT COUNT(*) FROM comments WHERE submission_id = ?",
                (foreign_submission,),
            ),
            0,
        )
        self.assertEqual(
            self.db_scalar(
                "SELECT COUNT(*) FROM submissions WHERE student_id = ?",
                (second["student"].id,),
            ),
            1,
        )

    # =====================================================================
    # 7. Admin metadata access is never Work or feedback body access.
    # =====================================================================

    def test_admin_metadata_is_not_content_or_feedback_access(self):
        ctx = self.new_course(submit=True)
        submission_id = ctx["submission"]["id"]
        self.comment(ctx["teacher_token"], submission_id, "teacher feedback")

        # Admin can manage metadata: create an account and edit a Playground.
        status, _, created = self.request(
            "POST",
            "/api/v1/admin/users",
            {
                "username": self.unique("managed"),
                "password": "secret",
                "role": "teacher",
            },
            token=ctx["admin_token"],
        )
        self.assertEqual(status, 201, created)
        self.assertEqual(created["user"]["role"], "teacher")

        status, _, body = self.request(
            "PATCH",
            f"/api/v1/playgrounds/{ctx['playground_id']}",
            {"policy": "public"},
            token=ctx["admin_token"],
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["playground"]["policy"], "public")

        # Admin still sees Submission metadata.
        status, _, body = self.get_submission(
            ctx["admin_token"], submission_id
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["submission"], ctx["submission"])
        status, _, listed = self.request(
            "GET",
            f"/api/v1/releases/{ctx['release']['id']}/submissions",
            token=ctx["admin_token"],
        )
        self.assertEqual(status, 200)
        self.assertIn(
            submission_id,
            [item["id"] for item in listed["submissions"]],
        )

        # But it is never Work content ...
        work_commit = ctx["work_commit_id"]
        for path in (
            f"/api/v1/playgrounds/{ctx['playground_id']}"
            f"/commits/{work_commit}/manifest",
            f"/api/v1/playgrounds/{ctx['playground_id']}"
            f"/commits/{work_commit}/files/work/main.py",
        ):
            with self.subTest(path=path):
                status, _, body = self.request(
                    "GET", path, token=ctx["admin_token"]
                )
                self.assertEqual((status, body["code"]), (404, "not_found"))

        # ... and never feedback body access.
        status, _, body = self.get_comments(ctx["admin_token"], submission_id)
        self.assertEqual((status, body["code"]), (403, "forbidden"))
        status, _, body = self.comment_raw(
            ctx["admin_token"],
            submission_id,
            "admin write",
            self.unique("k"),
        )
        self.assertEqual((status, body["code"]), (403, "forbidden"))
        self.assertEqual(
            self.db_scalar(
                "SELECT COUNT(*) FROM comments WHERE submission_id = ?",
                (submission_id,),
            ),
            1,
        )

    # =====================================================================
    # 8. Stale Work sync/restore never rewinds or merges.
    # =====================================================================

    def test_stale_work_operations_never_rewind_or_merge(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)
        student = self.seed_user("student")
        student_token = self.login_token(student.username)

        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        self.set_policy(admin_token, playground_id, "public")
        playground_commit = self.publish(
            admin_token, playground_id
        )["commitId"]

        first = self.sync(
            student_token,
            playground_id,
            playground_commit,
            {"work/main.py": b"a"},
        )
        second = self.sync(
            student_token,
            playground_id,
            playground_commit,
            {"work/main.py": b"b"},
            base=first,
        )
        third = self.sync(
            student_token,
            playground_id,
            playground_commit,
            {"work/main.py": b"c"},
            base=second,
        )
        self.assertEqual(
            self.work_head(playground_id, student.id), third
        )

        # A stale sync is rejected and leaves the ref at the current HEAD.
        status, _, body = self.sync_raw(
            student_token,
            playground_id,
            playground_commit,
            {"work/main.py": b"stale"},
            base=first,
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "work_head_changed")
        self.assertEqual(body["currentHeadCommitId"], third)
        self.assertEqual(
            self.work_head(playground_id, student.id), third
        )

        # A stale restore is rejected and leaves the ref at the current HEAD.
        status, _, body = self.request(
            "POST",
            f"/api/v1/works/{playground_id}/restore",
            {"baseCommitId": first, "sourceCommitId": first},
            token=student_token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "work_head_changed")
        self.assertEqual(body["currentHeadCommitId"], third)
        self.assertEqual(
            self.work_head(playground_id, student.id), third
        )

        # A successful restore appends with the current HEAD as sole parent.
        status, _, body = self.request(
            "POST",
            f"/api/v1/works/{playground_id}/restore",
            {"baseCommitId": third, "sourceCommitId": first},
            token=student_token,
        )
        self.assertEqual(status, 201, body)
        restored = body["workCommitId"]
        self.assertNotEqual(restored, third)
        self.assertEqual(self.commit_parents(restored), [third])
        self.assertEqual(self.tree_of(restored), self.tree_of(first))
        self.assertEqual(
            self.work_head(playground_id, student.id), restored
        )

        # The append preserves history: no commit moved backward and every
        # commit on the Work history has at most one parent (no merge).
        self.assertTrue(self.is_ancestor(third, restored))
        self.assertTrue(self.is_ancestor(first, restored))
        counts = self.parent_counts(restored)
        self.assertTrue(counts)
        self.assertLessEqual(max(counts.values()), 1)

    # =====================================================================
    # 9. Repeated creates are immutable; conflicting reuse is rejected.
    # =====================================================================

    def test_repeated_creates_are_immutable_and_conflicts_rejected(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)

        # --- publish -----------------------------------------------------
        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        key = self.unique("publish")
        status, _, first = self.publish_raw(
            admin_token, playground_id, key=key
        )
        self.assertEqual(status, 201)
        status, _, retry = self.publish_raw(
            admin_token, playground_id, key=key
        )
        self.assertEqual(status, 201)
        self.assertEqual(retry, first)
        self.assertEqual(
            self.db_scalar(
                "SELECT COUNT(*) FROM idempotency_results"
                " WHERE endpoint = ? AND request_key = ?",
                (
                    f"POST /api/v1/playgrounds/{playground_id}/commits",
                    key,
                ),
            ),
            1,
        )
        changed = self.publish_raw(
            admin_token,
            playground_id,
            (
                ("picopg.json", self.MANIFEST),
                ("course/module/app.py", b"print(2)"),
            ),
            key=key,
        )
        self.assertEqual((changed[0], changed[2]["code"]),
                         (409, "idempotency_conflict"))

        # --- submission --------------------------------------------------
        ctx = self.new_course()
        submit_key = self.unique("submit")
        submission = self.submit(
            ctx["student_token"],
            ctx["release"]["id"],
            ctx["work_commit_id"],
            submit_key,
        )
        retry_status, _, retry_body = self.submit_raw(
            ctx["student_token"],
            ctx["release"]["id"],
            ctx["work_commit_id"],
            submit_key,
        )
        self.assertEqual(retry_status, 201)
        self.assertEqual(retry_body["submission"], submission)

        second_work = self.sync(
            ctx["student_token"],
            ctx["playground_id"],
            ctx["commit_id"],
            {"work/main.py": b"print('second')"},
            base=ctx["work_commit_id"],
        )
        status, _, body = self.submit_raw(
            ctx["student_token"],
            ctx["release"]["id"],
            second_work,
            submit_key,
        )
        self.assertEqual((status, body["code"]),
                         (409, "idempotency_conflict"))
        self.assertEqual(
            self.db_scalar(
                "SELECT COUNT(*) FROM submissions"
                " WHERE release_id = ? AND student_id = ?",
                (ctx["release"]["id"], ctx["student"].id),
            ),
            1,
        )

        # --- comment -----------------------------------------------------
        comment_key = self.unique("comment")
        record = self.comment(
            ctx["teacher_token"],
            submission["id"],
            "feedback",
            comment_key,
        )
        retry_status, _, retry_body = self.comment_raw(
            ctx["teacher_token"],
            submission["id"],
            "feedback",
            comment_key,
        )
        self.assertEqual(retry_status, 201)
        self.assertEqual(retry_body["comment"], record)

        status, _, body = self.comment_raw(
            ctx["teacher_token"],
            submission["id"],
            "different feedback",
            comment_key,
        )
        self.assertEqual((status, body["code"]),
                         (409, "idempotency_conflict"))
        self.assertEqual(
            self.db_scalar(
                "SELECT COUNT(*) FROM comments WHERE submission_id = ?",
                (submission["id"],),
            ),
            1,
        )

    # =====================================================================
    # 10. Deleting and rebuilding the generated cache changes no IDs.
    # =====================================================================

    def test_generated_cache_rebuild_changes_no_authoritative_id(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)

        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        self.set_policy(admin_token, playground_id, "public")
        commit_id = self.publish(admin_token, playground_id)["commitId"]

        # A real artifact makes the rebuilt index non-trivial.
        (self.artifacts_path / "v1").mkdir(parents=True, exist_ok=True)
        (self.artifacts_path / "v1" / "runtime.exe").write_bytes(b"runtime")

        connection = open_database(self.database_path)
        try:
            before_payloads = generated.build_payloads(
                self.generated_path,
                connection=connection,
                repository=self.server.repository,
                artifacts_path=self.artifacts_path,
            )
            generated.rebuild(
                self.generated_path,
                connection=connection,
                repository=self.server.repository,
                artifacts_path=self.artifacts_path,
            )
            status, _, catalog_before = self.request(
                "GET", "/api/v1/catalog"
            )
            self.assertEqual(status, 200)
            status, _, manifest_before = self.request(
                "GET",
                f"/api/v1/playgrounds/{playground_id}"
                f"/commits/{commit_id}/manifest",
            )
            self.assertEqual(status, 200)

            # Delete the disposable cache and rebuild from authority.
            shutil.rmtree(self.generated_path)
            after_payloads = generated.build_payloads(
                self.generated_path,
                connection=connection,
                repository=self.server.repository,
                artifacts_path=self.artifacts_path,
            )
            generated.rebuild(
                self.generated_path,
                connection=connection,
                repository=self.server.repository,
                artifacts_path=self.artifacts_path,
            )
        finally:
            connection.close()

        # Every cache byte is reproducible from authority.
        self.assertEqual(after_payloads, before_payloads)
        self.assertEqual(
            sorted(path.name for path in self.generated_path.iterdir()),
            sorted(generated.GENERATED_FILENAMES),
        )

        # Authoritative IDs do not move: the Git HEAD is unchanged and the
        # live API keeps serving the same Playground/commit/SHA.
        self.assertEqual(
            manifests.current_playground_commit(
                self.server.repository, playground_id
            ),
            commit_id,
        )
        status, _, catalog_after = self.request("GET", "/api/v1/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(catalog_after, catalog_before)
        entries = {
            (item["playgroundId"], item["commitId"]): item
            for item in catalog_after["catalog"]
        }
        self.assertIn((playground_id, commit_id), entries)
        self.assertEqual(entries[(playground_id, commit_id)]["releaseIds"], [])

        status, _, manifest_after = self.request(
            "GET",
            f"/api/v1/playgrounds/{playground_id}"
            f"/commits/{commit_id}/manifest",
        )
        self.assertEqual(status, 200)
        self.assertEqual(manifest_after, manifest_before)
        expected_sha = hashlib.sha256(self.MANIFEST).hexdigest()
        files = {
            item["path"]: item for item in manifest_after["manifest"]["files"]
        }
        self.assertEqual(files["picopg.json"]["sha256"], expected_sha)


if __name__ == "__main__":
    unittest.main()
