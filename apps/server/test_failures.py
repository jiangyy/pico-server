"""Cross-store failure tests for Git, database, cache, and session boundaries.

Every test injects one deterministic failure at a single boundary between two
authoritative or disposable stores and then checks the authoritative state,
not merely the HTTP status:

1. A Playground ref compare-and-swap outage after the objects were written.
2. A generated-cache rebuild failure after Git and the database committed.
3. A failed idempotent database callback that must roll back completely.
4. An interrupted cache write before the atomic replacement.
5. Two concurrent Work syncs sharing one base.
6. A session that expires or is revoked between related calls.
"""

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

# Allow direct imports no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import accounts  # noqa: E402
import artifacts  # noqa: E402
import bundles  # noqa: E402
import classes  # noqa: E402
import generated  # noqa: E402
import manifests  # noqa: E402
import playgrounds  # noqa: E402
import releases  # noqa: E402
import repository  # noqa: E402
import server as server_module  # noqa: E402
import sessions  # noqa: E402
from database import open_database  # noqa: E402


MANIFEST_BYTES = b'{"schemaVersion":1,"runtime":"python"}'
BOOTSTRAP = accounts.User(
    id="seed-actor",
    username="seed-actor",
    role="admin",
    created_at="2026-01-01T00:00:00Z",
)
EXPIRED_AT = "2000-01-01T00:00:00.000000Z"


def commit_metadata(message="publish"):
    return repository.CommitMetadata(
        name="Test Author",
        email="author@example.test",
        timestamp="2026-01-01T00:00:00+00:00",
        message=message,
    )


class CrossStoreFailureTest(unittest.TestCase):
    """Git/database/cache/idempotency failure injection on real stores."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        base = Path(self.temporary_directory.name)
        self.database_path = base / "app.db"
        self.connection = open_database(self.database_path)
        self.addCleanup(self.connection.close)
        self.repository_path = base / "picopg.git"
        self.repository = repository.open_repository(self.repository_path)
        self.artifacts_path = base / "artifacts"
        self.generated_path = base / "generated"

        self.admin = accounts.create_user(
            self.connection,
            BOOTSTRAP,
            f"admin-{uuid.uuid4().hex[:8]}",
            "secret",
            "admin",
        )
        self.teacher = accounts.create_user(
            self.connection,
            self.admin,
            f"teacher-{uuid.uuid4().hex[:8]}",
            "secret",
            "teacher",
        )
        self.student = accounts.create_user(
            self.connection,
            self.admin,
            f"student-{uuid.uuid4().hex[:8]}",
            "secret",
            "student",
        )
        self.klass = classes.create_class(
            self.connection, self.admin, "Class", self.teacher.id
        )

    # ----- fixtures ------------------------------------------------------

    def make_bundle(self, course_data=b"print(1)"):
        return bundles.validate_bundle(
            [
                bundles.BundleEntry("picopg.json", MANIFEST_BYTES),
                bundles.BundleEntry("course/module/app.py", course_data),
            ]
        )

    def publish(self, playground_id, public=False):
        playgrounds.create_playground(
            self.connection, self.admin, playground_id
        )
        if public:
            playgrounds.update_playground(
                self.connection,
                self.admin,
                playground_id,
                {"policy": playgrounds.PUBLIC_POLICY},
            )
        return playgrounds.publish_playground(
            self.connection,
            self.admin,
            playground_id,
            self.repository,
            self.make_bundle(playground_id.encode("utf-8")),
            commit_metadata(f"publish {playground_id}"),
        )

    def rebuild(self):
        return generated.rebuild(
            self.generated_path,
            connection=self.connection,
            repository=self.repository,
            artifacts_path=self.artifacts_path,
        )

    def read_generated(self):
        return {
            filename: (self.generated_path / filename).read_bytes()
            for filename in generated.GENERATED_FILENAMES
            if (self.generated_path / filename).is_file()
        }

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
        return completed

    # ----- 1. Playground ref CAS fails after objects were written --------

    def test_playground_cas_failure_keeps_head_and_leaves_orphan_invisible(
        self
    ):
        playground_id = f"pg-{uuid.uuid4().hex[:8]}"
        winner = self.publish(playground_id).commit_id
        ref = self.repository.playground_ref(playground_id)

        created = []
        original_create = repository._create_commit

        def record_create(*arguments, **keywords):
            commit_id = original_create(*arguments, **keywords)
            created.append(commit_id)
            return commit_id

        original_run = repository._run_git

        def failing_run(arguments, cwd=None, stdin=None, environment=None):
            if arguments and arguments[0] == "update-ref":
                return subprocess.CompletedProcess(
                    list(arguments), 1, b"", b"cas failed"
                )
            return original_run(
                arguments, cwd=cwd, stdin=stdin, environment=environment
            )

        with mock.patch.object(
            repository, "_create_commit", side_effect=record_create
        ), mock.patch.object(repository, "_run_git", side_effect=failing_run):
            with self.assertRaises(repository.RepositoryError) as failure:
                playgrounds.publish_playground(
                    self.connection,
                    self.admin,
                    playground_id,
                    self.repository,
                    self.make_bundle(b"print(2)"),
                    commit_metadata("losing publish"),
                )

        # The injected failure leaves the protected ref unchanged, so this is
        # an operational update-ref failure rather than a lost CAS race.  It
        # must not be classified as a Playground head change.
        self.assertNotIsInstance(
            failure.exception, repository.PlaygroundHeadChangedError
        )

        # The winner HEAD is untouched and authoritative.
        self.assertEqual(
            self.git("rev-parse", ref).stdout.strip().decode("ascii"),
            winner,
        )
        self.assertEqual(
            manifests.current_playground_commit(
                self.repository, playground_id
            ),
            winner,
        )

        # The failed attempt really did create a new commit object, but no
        # ref reaches it, so it is not a visible record.
        self.assertEqual(len(created), 1)
        orphan = created[0]
        self.assertNotEqual(orphan, winner)
        self.assertEqual(
            self.git("cat-file", "-t", orphan).stdout.strip(), b"commit"
        )
        reachable = set(self.git("rev-list", "--all").stdout.split())
        self.assertNotIn(orphan.encode("ascii"), reachable)
        refs = set(
            self.git(
                "for-each-ref", "--format=%(objectname)"
            ).stdout.split()
        )
        self.assertNotIn(orphan.encode("ascii"), refs)

        # A retry after the injected outage is safe and appends to the winner.
        retry = playgrounds.publish_playground(
            self.connection,
            self.admin,
            playground_id,
            self.repository,
            self.make_bundle(b"print(3)"),
            commit_metadata("retry publish"),
        )
        self.assertFalse(retry.no_op)
        self.assertEqual(
            self.git("rev-parse", f"{retry.commit_id}^")
            .stdout.strip()
            .decode("ascii"),
            winner,
        )
        self.assertEqual(
            self.git("rev-parse", ref).stdout.strip().decode("ascii"),
            retry.commit_id,
        )

    # ----- 2. Git/database commit succeeds, cache generation fails -------

    def test_cache_failure_after_git_commit_keeps_authority_and_rebuilds(
        self
    ):
        first_id = f"pg-{uuid.uuid4().hex[:8]}"
        self.publish(first_id, public=True)
        self.rebuild()
        before = self.read_generated()

        second_id = f"pg-{uuid.uuid4().hex[:8]}"
        second_commit = self.publish(second_id, public=True).commit_id

        # The authoritative stores already hold the new commit.
        self.assertEqual(
            manifests.current_playground_commit(
                self.repository, second_id
            ),
            second_commit,
        )

        with mock.patch.object(
            manifests,
            "generate_manifest",
            side_effect=manifests.ManifestError(manifests.REASON_MISSING_BLOB),
        ):
            with self.assertRaises(manifests.ManifestError):
                self.rebuild()

        # The previous complete cache is retained and no partial sibling is
        # left behind for a reader to pick up.
        self.assertEqual(self.read_generated(), before)
        self.assertEqual(
            sorted(path.name for path in self.generated_path.iterdir()),
            sorted(generated.GENERATED_FILENAMES),
        )

        # The authoritative response for the new commit is still valid.
        manifest = manifests.generate_manifest(
            self.connection,
            None,
            self.repository,
            second_id,
            second_commit,
        )
        self.assertTrue(manifest.files)

        # Once the transient failure is gone the cache rebuilds completely.
        rebuilt = self.rebuild()
        catalog = json.loads(rebuilt[generated.CATALOG_FILENAME])["catalog"]
        self.assertEqual(
            {item["playgroundId"] for item in catalog},
            {first_id, second_id},
        )
        self.assertEqual(self.read_generated(), rebuilt)

    # ----- 3. Idempotent callback fails --------------------------------

    def test_failed_idempotent_callback_rolls_back_business_and_result(self):
        playground_id = f"pg-{uuid.uuid4().hex[:8]}"
        commit_id = self.publish(playground_id).commit_id
        request_key = uuid.uuid4().hex

        original_insert = releases._insert_release

        def insert_then_fail(*arguments, **keywords):
            original_insert(*arguments, **keywords)
            raise RuntimeError("business callback interrupted")

        with mock.patch.object(
            releases, "_insert_release", side_effect=insert_then_fail
        ):
            with self.assertRaises(RuntimeError):
                releases.create_release(
                    self.connection,
                    self.teacher,
                    self.repository,
                    self.klass.id,
                    playground_id,
                    commit_id,
                    "2026-01-01T00:00:00+00:00",
                    False,
                    request_key,
                )

        self.assertEqual(self.count("releases"), 0)
        self.assertEqual(self.count("idempotency_results"), 0)

        # The key was not consumed: the exact request can be retried and the
        # business row plus the recorded result now commit together.
        status, body = releases.create_release(
            self.connection,
            self.teacher,
            self.repository,
            self.klass.id,
            playground_id,
            commit_id,
            "2026-01-01T00:00:00+00:00",
            False,
            request_key,
        )
        self.assertEqual(status, 201)
        self.assertEqual(self.count("releases"), 1)
        self.assertEqual(self.count("idempotency_results"), 1)

        replay_status, replay_body = releases.create_release(
            self.connection,
            self.teacher,
            self.repository,
            self.klass.id,
            playground_id,
            commit_id,
            "2026-01-01T00:00:00+00:00",
            False,
            request_key,
        )
        self.assertEqual(replay_status, 201)
        self.assertEqual(replay_body, body)
        self.assertEqual(self.count("releases"), 1)

    def count(self, table):
        row = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM {table}"
        ).fetchone()
        return row["total"]

    # ----- 4. Interrupted cache write before atomic replace --------------

    def test_interrupted_cache_write_keeps_complete_old_file(self):
        target = self.generated_path / generated.CATALOG_FILENAME
        old = b'{"catalog":[]}'
        generated.atomic_write(target, old)

        with mock.patch.object(
            generated.os, "fsync", side_effect=OSError("write interrupted")
        ):
            with self.assertRaises(OSError):
                generated.atomic_write(target, b'{"catalog":["partial"]')

        self.assertEqual(target.read_bytes(), old)
        self.assertEqual(
            sorted(path.name for path in target.parent.iterdir()),
            [generated.CATALOG_FILENAME],
        )

    def test_interrupted_replace_keeps_complete_old_file(self):
        target = self.generated_path / generated.CATALOG_FILENAME
        old = b'{"catalog":[]}'
        generated.atomic_write(target, old)

        with mock.patch.object(
            generated.os, "replace", side_effect=OSError("replace interrupted")
        ):
            with self.assertRaises(OSError):
                generated.atomic_write(target, b'{"catalog":["new"]}')

        # The old complete file is served and no partial temporary file exists.
        self.assertEqual(target.read_bytes(), old)
        self.assertEqual(
            sorted(path.name for path in target.parent.iterdir()),
            [generated.CATALOG_FILENAME],
        )


class HttpFailureTestBase(unittest.TestCase):
    """Start one ephemeral threaded server against a temporary data dir."""

    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.database_path = Path(cls.temporary_directory.name) / "app.db"
        cls.repository_path = (
            Path(cls.temporary_directory.name) / "picopg.git"
        )
        cls.server = server_module.make_server(
            host="127.0.0.1",
            port=0,
            database_path=cls.database_path,
            repository_path=cls.repository_path,
            audit_output=io.StringIO(),
        )
        cls.port = cls.server.server_address[1]
        cls.repository = cls.server.repository
        cls.connection = open_database(cls.database_path)
        cls.admin = accounts.create_user(
            cls.connection,
            BOOTSTRAP,
            f"admin-{uuid.uuid4().hex[:8]}",
            "secret",
            "admin",
        )
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.connection.close()
        cls.temporary_directory.cleanup()

    # ----- HTTP helpers --------------------------------------------------

    def request(self, method, path, payload=None, token=None):
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        connection = HTTPConnection("127.0.0.1", self.port, timeout=15)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        connection.close()
        decoded = json.loads(raw) if raw else None
        return status, decoded

    def login(self, username):
        status, body = self.request(
            "POST",
            "/api/v1/login",
            {"username": username, "password": "secret"},
        )
        self.assertEqual(status, 200, body)
        return body["sessionToken"]

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
        return completed

    def read_work_tree(self, commit_id):
        entries = repository.list_commit_tree(self.repository, commit_id)
        blobs = repository.read_blobs(
            self.repository, [entry.object_id for entry in entries]
        )
        return {entry.path: blobs[entry.object_id] for entry in entries}


class WorkSyncConcurrencyFailureTest(HttpFailureTestBase):
    """One base, two concurrent Work syncs: one winner, one reported HEAD."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.student = accounts.create_user(
            cls.connection,
            cls.admin,
            f"student-{uuid.uuid4().hex[:8]}",
            "secret",
            "student",
        )
        cls.playground_id = f"pg-{uuid.uuid4().hex[:8]}"
        playgrounds.create_playground(
            cls.connection, cls.admin, cls.playground_id
        )
        playgrounds.update_playground(
            cls.connection,
            cls.admin,
            cls.playground_id,
            {"policy": playgrounds.PUBLIC_POLICY},
        )
        bundle = bundles.validate_bundle(
            [
                bundles.BundleEntry("picopg.json", MANIFEST_BYTES),
                bundles.BundleEntry("course/app.py", b"print(1)"),
            ]
        )
        result = playgrounds.publish_playground(
            cls.connection,
            cls.admin,
            cls.playground_id,
            cls.repository,
            bundle,
            commit_metadata("publish concurrency"),
        )
        cls.playground_commit = result.commit_id

    def test_two_concurrent_syncs_one_base_keep_every_input(self):
        token = self.login(self.student.username)
        path = f"/api/v1/works/{self.playground_id}/sync"
        payloads = {
            "alpha": {
                "playgroundCommitId": self.playground_commit,
                "baseCommitId": None,
                "files": [
                    {
                        "path": "work/a.py",
                        "data": base64.b64encode(b"alpha").decode("ascii"),
                    }
                ],
            },
            "beta": {
                "playgroundCommitId": self.playground_commit,
                "baseCommitId": None,
                "files": [
                    {
                        "path": "work/b.py",
                        "data": base64.b64encode(b"beta").decode("ascii"),
                    }
                ],
            },
        }

        barrier = threading.Barrier(2)
        results = {}

        def worker(name):
            barrier.wait(timeout=10)
            results[name] = self.request(
                "POST", path, payloads[name], token=token
            )

        threads = [
            threading.Thread(target=worker, args=(name,))
            for name in payloads
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(set(results), set(payloads))
        statuses = {name: results[name][0] for name in results}
        self.assertEqual(sorted(statuses.values()), [201, 409], results)

        winner = next(
            name for name, status in statuses.items() if status == 201
        )
        loser = next(
            name for name, status in statuses.items() if status == 409
        )
        winner_commit = results[winner][1]["workCommitId"]
        loser_body = results[loser][1]

        self.assertEqual(loser_body["code"], "work_head_changed")
        self.assertEqual(loser_body["currentHeadCommitId"], winner_commit)

        # The ref holds exactly the winner commit; the loser's request was
        # never partially applied.
        ref = self.repository.work_ref(
            self.playground_id, self.student.id
        )
        self.assertEqual(
            self.git("rev-parse", ref).stdout.strip().decode("ascii"),
            winner_commit,
        )
        self.assertEqual(
            self.git("rev-parse", f"{winner_commit}^")
            .stdout.strip()
            .decode("ascii"),
            self.playground_commit,
        )

        # The winner's own input is intact in the authoritative tree.
        winner_path = "work/a.py" if winner == "alpha" else "work/b.py"
        winner_content = b"alpha" if winner == "alpha" else b"beta"
        self.assertEqual(
            self.read_work_tree(winner_commit)[winner_path], winner_content
        )

        # The loser retries on top of the reported winner HEAD with its own
        # input plus the winner's retained input; both survive.
        retry_files = [
            {
                "path": "work/a.py",
                "data": base64.b64encode(b"alpha").decode("ascii"),
            },
            {
                "path": "work/b.py",
                "data": base64.b64encode(b"beta").decode("ascii"),
            },
        ]
        status, body = self.request(
            "POST",
            path,
            {
                "playgroundCommitId": self.playground_commit,
                "baseCommitId": winner_commit,
                "files": retry_files,
            },
            token=token,
        )
        self.assertEqual(status, 200, body)
        final_tree = self.read_work_tree(body["workCommitId"])
        self.assertEqual(final_tree["work/a.py"], b"alpha")
        self.assertEqual(final_tree["work/b.py"], b"beta")


class SessionBoundaryFailureTest(HttpFailureTestBase):
    """An invalidated session must not reach the write path."""

    def test_revoked_session_between_writes_performs_no_write(self):
        token = self.login(self.admin.username)
        first_id = f"pg-{uuid.uuid4().hex[:8]}"
        status, body = self.request(
            "POST", "/api/v1/playgrounds", {"id": first_id}, token=token
        )
        self.assertEqual(status, 201, body)

        self.assertTrue(sessions.revoke_session(self.connection, token))

        second_id = f"pg-{uuid.uuid4().hex[:8]}"
        status, body = self.request(
            "POST", "/api/v1/playgrounds", {"id": second_id}, token=token
        )
        self.assertEqual(status, 401, body)
        self.assertEqual(body["code"], "invalid_session")

        rows = {
            row["id"]
            for row in self.connection.execute(
                "SELECT id FROM playgrounds"
            )
        }
        self.assertIn(first_id, rows)
        self.assertNotIn(second_id, rows)

    def test_expired_session_between_writes_performs_no_write(self):
        token = self.login(self.admin.username)
        first_id = f"pg-{uuid.uuid4().hex[:8]}"
        status, body = self.request(
            "POST", "/api/v1/playgrounds", {"id": first_id}, token=token
        )
        self.assertEqual(status, 201, body)

        with self.connection:
            self.connection.execute(
                "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                (EXPIRED_AT, sessions.hash_session_token(token)),
            )

        second_id = f"pg-{uuid.uuid4().hex[:8]}"
        status, body = self.request(
            "POST", "/api/v1/playgrounds", {"id": second_id}, token=token
        )
        self.assertEqual(status, 401, body)
        self.assertEqual(body["code"], "invalid_session")

        rows = {
            row["id"]
            for row in self.connection.execute(
                "SELECT id FROM playgrounds"
            )
        }
        self.assertIn(first_id, rows)
        self.assertNotIn(second_id, rows)


if __name__ == "__main__":
    unittest.main(verbosity=2)
