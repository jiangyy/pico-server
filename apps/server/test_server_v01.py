"""Black-box acceptance gate for PicoPG Server v0.1."""

import hashlib
import io
import json
import os
import sys
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import maintenance  # noqa: E402
import repository  # noqa: E402
import server  # noqa: E402
import test_api  # noqa: E402
import test_invariants  # noqa: E402
import test_maintenance  # noqa: E402


class ServerV01AcceptanceTest(test_invariants.EndToEndInvariantTest):
    """Integrated positive and adversarial flows over the real HTTP server."""

    def create_account(self, admin_token, role):
        username = self.unique(role)
        status, _, body = self.request(
            "POST",
            "/api/v1/admin/users",
            {"username": username, "password": "secret", "role": role},
            token=admin_token,
        )
        self.assertEqual(status, 201, body)
        return body["user"], self.login_token(username)

    def test_complete_server_v01_flow(self):
        runtime = b"synthetic-runtime-v01"
        version = self.unique("runtime")
        version_dir = self.artifacts_path / version
        version_dir.mkdir(parents=True)
        (version_dir / "runtime.bin").write_bytes(runtime)

        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)
        teacher, teacher_token = self.create_account(admin_token, "teacher")
        student, student_token = self.create_account(admin_token, "student")

        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)
        self.set_policy(admin_token, playground_id, "public")
        published = self.publish(
            admin_token,
            playground_id,
            (
                ("picopg.json", self.MANIFEST),
                ("course/main.py", b"print('course')\n"),
            ),
        )
        playground_commit = published["commitId"]

        klass = self.create_class(admin_token, teacher["id"])
        self.assign_student(admin_token, klass["id"], student["id"])
        release = self.release(
            teacher_token,
            klass["id"],
            self.release_payload(
                playground_id,
                playground_commit,
                requires_submission=True,
            ),
        )

        status, _, users = self.request(
            "GET", "/api/v1/admin/users?limit=100", token=admin_token
        )
        self.assertEqual(status, 200)
        self.assertTrue(
            {teacher["id"], student["id"]}.issubset(
                {item["userId"] for item in users["users"]}
            )
        )
        status, _, playgrounds = self.request(
            "GET", "/api/v1/admin/playgrounds", token=admin_token
        )
        self.assertEqual(status, 200)
        self.assertIn(
            playground_id,
            {item["playgroundId"] for item in playgrounds["playgrounds"]},
        )

        status, bootstrap_headers, bootstrap_bytes = self.raw(
            "GET", "/api/v1/bootstrap"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            bootstrap_headers["etag"],
            f'"{hashlib.sha256(bootstrap_bytes).hexdigest()}"',
        )
        status, artifact_headers, artifact_bytes = self.raw(
            "GET", f"/api/v1/artifacts/{version}"
        )
        self.assertEqual((status, artifact_bytes), (200, runtime))
        self.assertEqual(
            artifact_headers["etag"],
            f'"{hashlib.sha256(runtime).hexdigest()}"',
        )

        status, _, catalog = self.request(
            "GET", "/api/v1/catalog", token=student_token
        )
        self.assertEqual(status, 200)
        self.assertIn(
            (playground_id, playground_commit),
            {
                (item["playgroundId"], item["commitId"])
                for item in catalog["catalog"]
            },
        )
        status, _, manifest = self.manifest(
            student_token, playground_id, playground_commit
        )
        self.assertEqual(status, 200)
        self.assertEqual(manifest["manifest"]["commitId"], playground_commit)
        status, _, course = self.commit_file(
            student_token,
            playground_id,
            playground_commit,
            "course/main.py",
        )
        self.assertEqual((status, course), (200, b"print('course')\n"))

        first = self.sync(
            student_token,
            playground_id,
            playground_commit,
            {"work/main.py": b"first\n"},
        )
        second = self.sync(
            student_token,
            playground_id,
            playground_commit,
            {"work/main.py": b"second\n"},
            base=first,
        )
        status, _, head = self.request(
            "GET", f"/api/v1/works/{playground_id}/head", token=student_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(head["work"]["workCommitId"], second)
        status, _, history = self.request(
            "GET",
            f"/api/v1/works/{playground_id}/history?limit=1",
            token=student_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(history["commits"][0]["workCommitId"], second)
        self.assertEqual(history["nextCursor"], second)
        status, _, work_manifest = self.request(
            "GET",
            f"/api/v1/works/{playground_id}/commits/{second}/manifest",
            token=student_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(work_manifest["manifest"]["workCommitId"], second)
        status, _, work_bytes = self.raw(
            "GET",
            f"/api/v1/works/{playground_id}/commits/{second}/files/main.py",
            token=student_token,
        )
        self.assertEqual((status, work_bytes), (200, b"second\n"))

        status, _, stale = self.sync_raw(
            student_token,
            playground_id,
            playground_commit,
            {"work/main.py": b"stale\n"},
            base=first,
        )
        self.assertEqual((status, stale["code"]), (409, "work_head_changed"))
        self.assertEqual(stale["currentHeadCommitId"], second)
        submitted_work = self.sync(
            student_token,
            playground_id,
            playground_commit,
            {"work/main.py": b"submitted\n"},
            base=stale["currentHeadCommitId"],
        )
        submission = self.submit(
            student_token,
            release["id"],
            submitted_work,
            self.unique("submission"),
        )
        newer_work = self.sync(
            student_token,
            playground_id,
            playground_commit,
            {"work/main.py": b"newer\n"},
            base=submitted_work,
        )
        self.assertNotEqual(newer_work, submitted_work)

        status, _, listed = self.request(
            "GET",
            f"/api/v1/releases/{release['id']}/submissions",
            token=teacher_token,
        )
        self.assertEqual(status, 200)
        self.assertIn(submission["id"], {item["id"] for item in listed["submissions"]})
        snapshot_path = (
            f"/api/v1/submissions/{submission['id']}/work/files/main.py"
        )
        status, _, snapshot = self.raw("GET", snapshot_path, token=teacher_token)
        self.assertEqual((status, snapshot), (200, b"submitted\n"))
        comment = self.comment(
            teacher_token, submission["id"], "reviewed", self.unique("comment")
        )
        status, _, comments = self.get_comments(student_token, submission["id"])
        self.assertEqual(status, 200)
        self.assertEqual(comments["comments"][0]["id"], comment["id"])
        status, _, student_snapshot = self.raw(
            "GET", snapshot_path, token=student_token
        )
        self.assertEqual((status, student_snapshot), (200, b"submitted\n"))

        status, _, metadata = self.get_submission(admin_token, submission["id"])
        self.assertEqual(status, 200)
        self.assertEqual(metadata["submission"]["workCommitId"], submitted_work)
        status, _, denied = self.raw("GET", snapshot_path, token=admin_token)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(denied)["code"], "not_found")
        status, _, denied = self.get_comments(admin_token, submission["id"])
        self.assertEqual((status, denied["code"]), (403, "forbidden"))

    def test_integrated_adversarial_boundaries(self):
        admin = self.seed_user("admin")
        admin_token = self.login_token(admin.username)
        playground_id = self.unique("pg")
        self.create_playground(admin_token, playground_id)

        invalid_manifests = (
            {"schemaVersion": "1", "runtime": "python"},
            {"schemaVersion": 1.0, "runtime": "python"},
            {"schemaVersion": 1, "runtime": "Python"},
            {"schemaVersion": 1, "runtime": "python3"},
            {"schemaVersion": 1, "runtime": "pyodide"},
            {"schemaVersion": 1, "runtime": "cpython"},
        )
        for document in invalid_manifests:
            with self.subTest(document=document):
                status, _, body = self.publish_raw(
                    admin_token,
                    playground_id,
                    (("picopg.json", json.dumps(document).encode()),),
                )
                self.assertEqual((status, body["code"]), (400, "invalid_bundle"))

        invalid_paths = (
            (("course", b"file"), ("course/main.py", b"nested")),
            (("course/A.py", b"a"), ("course/a.py", b"b")),
            (("course/CON.py", b"reserved"),),
        )
        for entries in invalid_paths:
            with self.subTest(entries=entries):
                status, _, body = self.publish_raw(
                    admin_token,
                    playground_id,
                    (("picopg.json", self.MANIFEST), *entries),
                )
                self.assertEqual((status, body["code"]), (400, "invalid_bundle"))

        for malformed in ("../bad", "bad/segment", ".lock", "a" * 129):
            status, _, body = self.request(
                "POST",
                "/api/v1/playgrounds",
                {"id": malformed},
                token=admin_token,
            )
            self.assertEqual((status, body["code"]), (400, "invalid_request"))

        trace = Path(self.temporary_directory.name) / "git-trace"
        hooks = Path(self.temporary_directory.name) / "hooks"
        hooks.mkdir(exist_ok=True)
        marker = hooks / "ran"
        hook = hooks / "reference-transaction"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        hook.chmod(0o755)
        with mock.patch.dict(
            os.environ,
            {
                "GIT_DIR": str(Path(self.temporary_directory.name) / "decoy"),
                "GIT_OBJECT_DIRECTORY": str(
                    Path(self.temporary_directory.name) / "objects"
                ),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": str(hooks),
                "GIT_TRACE": str(trace),
            },
        ):
            published = self.publish(admin_token, playground_id)
        self.assertTrue(published["commitId"])
        self.assertFalse(marker.exists())
        self.assertFalse(trace.exists())

        for path in (
            f"/api/v1/playgrounds/{playground_id}/commits/%ZZ/manifest",
            f"/api/v1/playgrounds/{playground_id}/commits/%2e%2e/manifest",
            f"/api/v1/playgrounds/{playground_id}/commits/a%2fb/manifest",
        ):
            status, _, body = self.request("GET", path, token=admin_token)
            self.assertIn(status, (400, 404), (path, body))
            self.assertIn(body["code"], ("invalid_request", "not_found"))

        private = "/private/acceptance-secret"
        with mock.patch.object(
            server,
            "create_playground",
            side_effect=repository.RepositoryError(private),
        ):
            status, _, body = self.request(
                "POST",
                "/api/v1/playgrounds",
                {"id": self.unique("pg")},
                token=admin_token,
            )
        self.assertEqual((status, body["code"]), (500, "internal_error"))
        self.assertNotIn(private, json.dumps(body))


class ServerV01PaginationAcceptance(test_api.ListPaginationApiTest):
    """Run the integrated pagination/cursor contract in the acceptance gate."""


class ServerV01RecoveryAcceptance(test_maintenance.MaintenanceTest):
    """Prove one real recovery set starts and serves equivalent public data."""

    def test_validate_compact_backup_restore_and_restart(self):
        refs_before = {
            ref: maintenance._resolve_ref(self.repository, ref)
            for ref in repository.list_refs(self.repository)
        }
        rows_before = {
            table: [tuple(row) for row in self.connection.execute(
                f"SELECT * FROM {table} ORDER BY id"
            ).fetchall()]
            for table in ("users", "classes", "releases", "submissions")
        }
        generated_before = (self.generated_path / "catalog.json").read_bytes()

        summary = maintenance.validate_data_dir(
            self.data_dir, service_stopped=True
        )
        self.assertEqual(summary["releases"], 1)
        maintenance.compact_repository(self.data_dir, service_stopped=True)
        self.assertEqual(
            {
                ref: maintenance._resolve_ref(self.repository, ref)
                for ref in repository.list_refs(self.repository)
            },
            refs_before,
        )
        self.assertEqual(
            {
                table: [tuple(row) for row in self.connection.execute(
                    f"SELECT * FROM {table} ORDER BY id"
                ).fetchall()]
                for table in ("users", "classes", "releases", "submissions")
            },
            rows_before,
        )
        self.assertEqual(
            (self.generated_path / "catalog.json").read_bytes(),
            generated_before,
        )

        maintenance.create_backup(
            self.data_dir, self.backup_path, service_stopped=True
        )
        maintenance.validate_backup(self.backup_path)
        restore = self.base / "restored"
        maintenance.restore_backup(
            self.backup_path, restore, service_stopped=True
        )

        restored_server = server.make_server(
            host="127.0.0.1",
            port=0,
            database_path=restore / "app.db",
            repository_path=restore / "picopg.git",
            artifacts_path=restore / "artifacts",
            generated_path=restore / "generated",
            audit_output=io.StringIO(),
        )
        thread = threading.Thread(
            target=restored_server.serve_forever, daemon=True
        )
        thread.start()
        try:
            connection = HTTPConnection(
                "127.0.0.1", restored_server.server_address[1], timeout=5
            )
            connection.request("GET", "/api/v1/catalog")
            response = connection.getresponse()
            body = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn(
                (self.playground_id, self.playground_commit_id),
                {
                    (item["playgroundId"], item["commitId"])
                    for item in body["catalog"]
                },
            )
        finally:
            restored_server.shutdown()
            restored_server.server_close()
            thread.join(timeout=5)

        for root in (
            self.data_dir,
            self.repository_path,
            self.artifacts_path,
            self.backup_path,
            restore,
            restore / "picopg.git",
            restore / "artifacts",
            restore / "generated",
        ):
            self.assertEqual(root.stat().st_mode & 0o077, 0, root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
