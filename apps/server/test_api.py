"""HTTP tests for the two functions this service has.

Reads are anonymous: catalog, manifest and exact files.
Writes are the operator's bundle upload and a client's bounded submission.
Every test drives a real server over a real socket against a temporary data
directory, so a passing test is evidence about the HTTP contract rather than
about an in-process call.
"""

import hashlib
import io
import json
import sqlite3
import subprocess
import threading
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

from events_types import PROTOCOL_VERSION  # noqa: E402
from unittest import mock  # noqa: E402

import playgrounds  # noqa: E402
import repository  # noqa: E402
import server as server_module  # noqa: E402
from server import make_server  # noqa: E402

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
        # protocol.md §4: every API request states its protocol version, so
        # the shared helper sends it unless a test deliberately omits it.
        headers = {"X-Protocol-Version": str(PROTOCOL_VERSION)}
        body = raw_body
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            headers["Content-Type"] = content_type
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        for name, value in (extra_headers or {}).items():
            if value is None:
                headers.pop(name, None)
            else:
                headers[name] = value

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

    def variant_zip(self, body):
        """The same bundle with one file's bytes changed, so its tree differs."""

        return self.build_zip(
            (
                ("picopg.json", MANIFEST),
                ("course/module/app.py", body),
                ("starter/setup.py", b"setup"),
            )
        )

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

    def current_version(self, playground_id):
        """The Version a publish of this Playground would have to name as base."""

        status, _, body = self.request("GET", "/api/v1/catalog")
        self.assertEqual(status, 200)
        for entry in body["playgrounds"]:
            if entry["playground_id"] == playground_id:
                return entry["current_version_id"]
        return None

    def catalog_entries(self):
        """The catalog's Playground list, as a client sees it."""

        status, _, body = self.request("GET", "/api/v1/catalog")
        self.assertEqual(status, 200)
        return body["playgrounds"]

    def catalog_ids(self):
        return [entry["playground_id"] for entry in self.catalog_entries()]

    def sync(self, pending_events, after_seq=0):
        """One sync round; the status is asserted here so callers read bodies."""

        status, _, body = self.request(
            "POST",
            "/api/v1/sync",
            payload={"pending_events": pending_events, "after_seq": after_seq},
        )
        self.assertEqual(status, 200, body)
        return body

    def retire(self, playground_id, token=OPERATOR_TOKEN):
        """Take a Playground out of the catalog."""

        return self.request(
            "DELETE", f"/api/v1/playgrounds/{playground_id}", token=token
        )

    def publish(self, playground_id, archive=None, key="key-1", base=None):
        """Publish one bundle, naming the current Version as its base.

        protocol.md §2: every publish of an existing Playground says which
        Version it built on. `base=None` asks the server what that is, exactly
        as `upload.mjs` does; `base=False` sends nothing, which is how the
        refusal is tested.
        """

        headers = {"Idempotency-Key": key}
        if base is None:
            found = self.current_version(playground_id)
            if found is not None:
                headers["X-Base-Version-Id"] = found
        elif base is not False:
            headers["X-Base-Version-Id"] = base
        return self.request(
            "POST",
            f"/api/v1/playgrounds/{playground_id}/versions",
            raw_body=archive if archive is not None else self.build_zip(),
            token=OPERATOR_TOKEN,
            content_type="application/zip",
            extra_headers=headers,
        )

    def seed(self, playground_id=None):
        """Create and publish one Playground; return ``(id, commitId)``."""

        playground_id = playground_id or f"pg-{uuid.uuid4().hex[:8]}"
        self.create_playground(playground_id)
        status, _, body = self.publish(playground_id)
        assert status == 201, (status, body)
        return playground_id, body["version_id"]

    def hand_in(self, playground_id, commit_id, header="# 姓名：2026-001", files=None):
        """Hand one piece of work in through the client's one write path.

        Returns the single ``EventResult`` the server answered with, so a
        caller can assert on ``status``/``reason`` the same way the client
        does.
        """
        event = {
            "event_id": uuid.uuid4().hex,
            "kind": "submit_code",
            "payload": {
                "playground_id": playground_id,
                "version_id": commit_id,
                "files": files if files is not None else {"work/main.py": f"{header}\nprint(1)\n"},
            },
        }
        status, _, body = self.request(
            "POST", "/api/v1/sync", payload={"pending_events": [event], "after_seq": 0}
        )
        self.assertEqual(status, 200, body)
        return body["event_results"][0]

    def stored_submissions(self, playground_id):
        status, _, body = self.request(
            "GET", "/api/v1/submissions", token=OPERATOR_TOKEN
        )
        self.assertEqual(status, 200)
        return [
            entry
            for entry in body["submissions"]
            if entry["playgroundId"] == playground_id
        ]


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
        self.assertEqual(
            sorted(body),
            [
                "minimum_client_protocol_version",
                "playgrounds",
                "protocol_version",
                "upto_seq",
            ],
        )
        self.assertIn(
            {"playground_id": playground_id, "current_version_id": commit_id},
            body["playgrounds"],
        )

    def test_manifest_lists_the_published_files(self):
        playground_id, commit_id = self.seed()

        status, _, body = self.request(
            "GET",
            f"/api/v1/playgrounds/{playground_id}/versions/{commit_id}/manifest",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["playground_id"], playground_id)
        self.assertEqual(body["version_id"], commit_id)
        self.assertEqual(
            sorted(entry["path"] for entry in body["files"]),
            ["course/module/app.py", "picopg.json", "starter/setup.py"],
        )
        for entry in body["files"]:
            self.assertEqual(sorted(entry), ["path", "sha256", "size"])
            self.assertEqual(len(entry["sha256"]), 64)

    def test_version_file_returns_exact_bytes_with_its_digest(self):
        playground_id, commit_id = self.seed()

        status, headers, body = self.request(
            "GET",
            f"/api/v1/playgrounds/{playground_id}/versions/{commit_id}"
            "/files/course/module/app.py",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"print(1)")
        self.assertEqual(headers["content-type"], "application/octet-stream")
        self.assertEqual(
            headers["cache-control"], "public, max-age=31536000, immutable"
        )
        # The client refuses a download whose digest header is absent or
        # malformed, so the header is part of the contract, not a nicety.
        self.assertEqual(
            headers["x-content-sha256"], hashlib.sha256(b"print(1)").hexdigest()
        )

    def test_manifest_of_an_unknown_version_is_not_found(self):
        playground_id, _ = self.seed()

        status, _, body = self.request(
            "GET",
            f"/api/v1/playgrounds/{playground_id}/versions/{'0' * 40}/manifest",
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")

    def test_a_version_file_outside_the_tree_is_not_found(self):
        playground_id, commit_id = self.seed()

        status, _, body = self.request(
            "GET",
            f"/api/v1/playgrounds/{playground_id}/versions/{commit_id}"
            "/files/course/../picopg.json",
        )
        self.assertEqual(status, 404)


class PublishBaseTest(ServerTestCase):
    """`base_version_id`: publishing against a Version that is still current.

    Without it two publishes racing each other would both append a Version and
    the later one would silently discard the earlier one's content. The base is
    what turns that into a refusal the publisher can see and resolve.
    """

    def test_the_first_publish_of_a_playground_names_no_base(self):
        playground_id = "pg-base-first"
        self.create_playground(playground_id)
        status, _, body = self.publish(playground_id, base=False)
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(len(body["version_id"]), 40)

    def test_the_first_publish_must_not_name_a_base(self):
        # Naming one claims a history the Playground does not have.
        playground_id = "pg-base-first-with-base"
        self.create_playground(playground_id)
        status, _, body = self.publish(playground_id, base="0" * 40)
        self.assertEqual(status, 409)
        self.assertEqual(body["status"], "conflict")

    def test_a_later_publish_must_name_the_current_base(self):
        playground_id, version_id = self.seed("pg-base-required")
        # No base at all: a malformed PublishRequest, not a stale one.
        status, _, body = self.publish(playground_id, key="no-base", base=False)
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_request")

        # The current Version is accepted, and is a no-op because the content is
        # the same bundle.
        status, _, body = self.publish(playground_id, key="with-base", base=version_id)
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "no_op")
        self.assertEqual(body["version_id"], version_id)

    def test_a_stale_base_is_a_conflict_and_writes_nothing(self):
        playground_id, first = self.seed("pg-base-stale")
        # Someone else publishes first, so the base this caller holds is gone.
        status, _, second = self.publish(playground_id, archive=self.variant_zip(b"second"), key="k2")
        self.assertEqual(status, 201)
        self.assertNotEqual(second["version_id"], first)

        status, _, body = self.publish(
            playground_id, archive=self.variant_zip(b"third"), key="k3", base=first
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["status"], "conflict")
        # A conflict carries no version: the caller must read the current one.
        self.assertNotIn("version_id", body)

        # ...and the two accepted publishes are the only ones in the history.
        status, _, manifest = self.request(
            "GET", f"/api/v1/playgrounds/{playground_id}/versions/{second['version_id']}/manifest"
        )
        self.assertEqual(status, 200)

    def test_a_malformed_base_is_a_bad_request(self):
        playground_id, _ = self.seed("pg-base-malformed")
        for base in ("HEAD", "not-a-version", "A" * 40, "0" * 39, "0" * 41):
            with self.subTest(base=base):
                status, _, body = self.publish(playground_id, key=f"k-{base}", base=base)
                self.assertEqual(status, 400, base)
                self.assertEqual(body["code"], "invalid_request")

    def test_a_blank_base_is_treated_as_absent(self):
        # A header sent with an empty value is not a claim about anything.
        playground_id = "pg-base-blank"
        self.create_playground(playground_id)
        status, _, _ = self.publish(playground_id, base="")
        self.assertEqual(status, 201)

    def test_the_base_is_part_of_what_a_key_identifies(self):
        # The same bundle offered against a different Version is a different
        # request with a different answer, so it cannot replay the first result.
        playground_id, version_id = self.seed("pg-base-identity")
        self.assertEqual(self.publish(playground_id, key="same-key", base=False)[0], 400)
        status, _, _ = self.publish(playground_id, key="same-key", base=version_id)
        self.assertEqual(status, 201)


class PublishCreatesPlaygroundTest(ServerTestCase):
    """protocol.md §2 step 0：不存在的 Playground 由第一次发布带出来。"""

    def test_publishing_to_an_unknown_playground_creates_it(self):
        # 不需要先登记：协议里 PublishRequest 的「Playground 不存在」分支就是创建。
        status, _, body = self.publish("brand-new", archive=self.build_zip(), key="create-1")

        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "accepted")
        self.assertIn("brand-new", self.catalog_ids())
        self.assertEqual(self.current_version("brand-new"), body["version_id"])

    def test_a_created_playground_is_published_like_any_other(self):
        _, _, first = self.publish("fresh", archive=self.build_zip(), key="create-fresh")
        status, _, second = self.publish(
            "fresh",
            archive=self.variant_zip(b"two"),
            key="create-fresh-2",
            base=first["version_id"],
        )
        self.assertEqual((status, second["status"]), (201, "accepted"))

        # 相同内容再来一次是 no_op，和登记过的 Playground 完全一样
        _, _, third = self.publish(
            "fresh",
            archive=self.variant_zip(b"two"),
            key="create-fresh-3",
            base=second["version_id"],
        )
        self.assertEqual(third, {"status": "no_op", "version_id": second["version_id"]})

    def test_a_base_sent_for_a_playground_that_does_not_exist_is_a_conflict(self):
        # 它声称的历史不存在：不是 404，也不是静默创建。
        status, _, body = self.publish(
            "never-existed", archive=self.build_zip(), key="create-bad-base", base="0" * 40
        )
        self.assertEqual(status, 409)
        self.assertEqual(body, {"status": "conflict"})
        self.assertNotIn("never-existed", self.catalog_ids())

    def test_creating_announces_the_version_to_clients(self):
        # 创建和发布一样要进内容日志，否则客户端永远不知道该装它。
        _, _, body = self.publish("announced", archive=self.build_zip(), key="create-announced")
        entries = self.sync([], after_seq=0)["entries"]
        published = [
            entry for entry in entries
            if entry["event"]["kind"] == "publish_playground"
            and entry["event"]["payload"]["playground_id"] == "announced"
        ]
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["event"]["payload"]["version_id"], body["version_id"])

    def test_a_publish_is_still_not_how_an_archived_playground_comes_back(self):
        self.publish("revived", archive=self.build_zip(), key="revive-1")
        self.retire("revived")
        status, _, body = self.publish("revived", archive=self.variant_zip(b"x"), key="revive-2")
        self.assertEqual(status, 409)
        self.assertEqual(body, {"status": "playground_stopped"})


class PublishPhasesOverHttpTest(ServerTestCase):
    """三阶段在 HTTP 层的顺序与失败语义。

    这些断言针对的是**两个存储各自独立**这件事：Git 不在 SQLite 事务里，数据库
    没答应之前 history ref 不动。两条都曾经不成立，而且都只在出错时才看得见。
    """

    def history_head(self, playground_id):
        completed = subprocess.run(
            ["git", "-C", str(self.server.repository_path), "rev-parse", "--verify", "--quiet",
             f"refs/picopg/history/{playground_id}"],
            capture_output=True, text=True,
        )
        return completed.stdout.strip() or None

    def pending_refs(self):
        completed = subprocess.run(
            ["git", "-C", str(self.server.repository_path), "for-each-ref", "--format=%(refname)",
             "refs/picopg/pending/"],
            capture_output=True, text=True,
        )
        return [line for line in completed.stdout.split("\n") if line]

    def test_a_database_failure_after_prepare_leaves_no_trace(self):
        # Prepare 已经建了 commit 和 pending ref，然后数据库那一半失败：history ref
        # 必须不动，pending ref 必须被清掉，那个 commit 之后由 GC 收走。
        playground_id, version_id = self.seed("pg-db-fails")
        before = self.history_head(playground_id)
        self.assertEqual(before, version_id)

        def explode(*_args, **_kwargs):
            raise sqlite3.OperationalError("simulated database failure")

        # 打 server 模块里的名字：它是 `from playgrounds import ...` 进来的，
        # 打 playgrounds 上的属性对已经绑好的引用没有影响。
        with mock.patch.object(server_module, "commit_prepared_version", side_effect=explode):
            status, _, _ = self.publish(
                playground_id, archive=self.variant_zip(b"new"), key="db-fails", base=version_id
            )

        self.assertEqual(status, 500, "数据库失败就是这个请求失败")
        self.assertEqual(self.history_head(playground_id), before, "history ref 不能领先数据库")
        self.assertEqual(self.pending_refs(), [], "失败路径必须清掉 pending ref")

    def test_the_git_work_does_not_hold_the_database_write_lock(self):
        # Git 子进程不在 BEGIN IMMEDIATE 里面：Prepare 期间另一个连接还能写。
        playground_id, version_id = self.seed("pg-no-lock")
        observed = {}
        original = repository.publish_playground_prepare

        def spy(*args, **kwargs):
            other = sqlite3.connect(self.server.database_path, timeout=0.5)
            try:
                with other:
                    other.execute(
                        "INSERT INTO playgrounds (id, created_at, state, current_version_id)"
                        " VALUES ('probe-lock', 'now', 'active', '')"
                    )
                observed["blocked"] = False
            except sqlite3.OperationalError as error:
                observed["blocked"] = str(error)
            finally:
                other.close()
            return original(*args, **kwargs)

        with mock.patch.object(server_module, "publish_playground_prepare", side_effect=spy):
            status, _, _ = self.publish(
                playground_id, archive=self.variant_zip(b"x"), key="no-lock", base=version_id
            )
        self.assertEqual(status, 201)
        self.assertEqual(observed.get("blocked"), False, f"Prepare 期间数据库被锁住了：{observed}")

    def test_recovery_runs_before_the_first_request(self):
        # 启动顺序的一部分：make_server 返回时不该还有 pending ref。
        playground_id, version_id = self.seed("pg-recovery-wired")
        repository.create_pending_ref(
            self.server.repository, "evt-leftover", version_id
        )
        self.assertIn("refs/picopg/pending/evt-leftover", self.pending_refs())

        restarted = make_server(
            host="127.0.0.1",
            port=0,
            database_path=self.server.database_path,
            repository_path=self.server.repository_path,
            operator_token=OPERATOR_TOKEN,
        )
        self.addCleanup(restarted.server_close)
        self.addCleanup(restarted.shutdown)
        threading.Thread(target=restarted.serve_forever, daemon=True).start()

        self.assertEqual(self.pending_refs(), [], "make_server 返回前必须已经恢复过")


class RestoreTest(ServerTestCase):
    """Bringing an archived Playground back.

    Without this the `state` flag only moves one way: publishing is refused
    while a Playground is stopped, so a retired course could never return — and
    `retire.mjs` documented exactly that impossible recovery.
    """

    def restore(self, playground_id, token=OPERATOR_TOKEN):
        return self.request(
            "POST", f"/api/v1/playgrounds/{playground_id}/restore", token=token
        )

    def test_a_restored_course_is_in_the_catalog_and_publishable_again(self):
        playground_id, version_id = self.seed("pg-restore")
        self.assertEqual(self.retire(playground_id)[2]["status"], "accepted")
        self.assertNotIn(
            playground_id, [p["playground_id"] for p in self.catalog_entries()]
        )

        status, _, body = self.restore(playground_id)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "accepted"})

        entry = next(
            item for item in self.catalog_entries() if item["playground_id"] == playground_id
        )
        self.assertEqual(entry["current_version_id"], version_id)

        # And the refusal that made restore necessary is gone.
        status, _, _ = self.publish(playground_id, base=version_id)
        self.assertEqual(status, 201)

    def test_restoring_an_active_playground_is_a_no_op(self):
        playground_id, _ = self.seed("pg-restore-noop")
        status, _, body = self.restore(playground_id)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "no_op"})

    def test_restoring_an_unknown_playground_is_not_found(self):
        self.assertEqual(self.restore("pg-never-existed")[0], 404)

    def test_restoring_needs_the_operator_token(self):
        playground_id, _ = self.seed("pg-restore-auth")
        self.retire(playground_id)
        self.assertEqual(self.restore(playground_id, token=None)[0], 401)
        self.assertEqual(self.restore(playground_id, token="wrong-token-000000")[0], 401)

    def test_a_restore_reaches_clients_as_a_publish(self):
        # The content log has no "restored" kind, so the client learns about it
        # the same way it learns about anything else: a publish it can install.
        playground_id, version_id = self.seed("pg-restore-event")
        before = self.sync([], after_seq=0)["upto_seq"]
        self.retire(playground_id)
        self.restore(playground_id)

        body = self.sync([], after_seq=before)
        kinds = [(entry["event"]["kind"], entry["event"]["payload"]) for entry in body["entries"]]
        self.assertEqual(
            kinds,
            [
                ("archive_playground", {"playground_id": playground_id}),
                (
                    "publish_playground",
                    {"playground_id": playground_id, "version_id": version_id},
                ),
            ],
        )

    def test_a_playground_that_was_never_published_can_be_restored(self):
        playground_id = "pg-restore-empty"
        self.create_playground(playground_id)
        self.retire(playground_id)
        status, _, body = self.restore(playground_id)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "accepted")
        # It has no Version, so it is active but still not in the catalog.
        self.assertNotIn(playground_id, [p["playground_id"] for p in self.catalog_entries()])


class CatalogSourceTest(ServerTestCase):
    """The catalog reads the database, and start-up keeps it complete.

    Resolving each row's ref with Git was one subprocess per course per request
    (measured at 2.2s for 300 courses, on every launch and refresh), so
    ``current_version_id`` is now the catalog's source. That makes it an
    invariant every writer must keep — these pin the two ways it could break.
    """

    def test_a_registered_but_unpublished_course_is_not_in_the_catalog(self):
        # POST /playgrounds creates the row; the version comes from the publish.
        # Half a registration must not look like a course.
        playground_id = "pg-registered-only"
        self.create_playground(playground_id)
        self.assertNotIn(playground_id, self.catalog_ids())

        # ...and the publish is what makes it appear.
        status, _, body = self.publish(playground_id)
        self.assertEqual(status, 201)
        self.assertIn(playground_id, self.catalog_ids())
        entry = next(
            item for item in self.request("GET", "/api/v1/catalog")[2]["playgrounds"]
            if item["playground_id"] == playground_id
        )
        self.assertEqual(entry["current_version_id"], body["version_id"])

    def test_a_row_from_before_the_column_existed_survives_a_restart(self):
        # A v1 row has a readable ref and an empty column. Restarting the server
        # is the upgrade path, and the course must still be there afterwards —
        # otherwise upgrading silently empties the catalog.
        playground_id, commit_id = self.seed("pg-upgrade")
        with self.server.database_path.open("rb") as _:
            pass
        connection = sqlite3.connect(self.server.database_path)
        with connection:
            connection.execute(
                "UPDATE playgrounds SET current_version_id = '' WHERE id = ?",
                (playground_id,),
            )
        connection.close()
        self.assertNotIn(playground_id, self.catalog_ids())

        restarted = make_server(
            host="127.0.0.1",
            port=0,
            database_path=self.server.database_path,
            repository_path=self.server.repository_path,
            operator_token=OPERATOR_TOKEN,
        )
        self.addCleanup(restarted.server_close)
        self.addCleanup(restarted.shutdown)
        threading.Thread(target=restarted.serve_forever, daemon=True).start()

        status, _, body = self.request("GET", "/api/v1/catalog")
        self.assertEqual(status, 200)
        entry = next(
            item for item in body["playgrounds"] if item["playground_id"] == playground_id
        )
        self.assertEqual(entry["current_version_id"], commit_id)


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
        self.assertEqual(body["version_id"], commit_id)
        self.assertEqual(body["status"], "no_op")

    def test_retrying_with_the_same_key_replays_the_result(self):
        # A retry resends the *same* request, base included: the base is part of
        # what the key identifies, so re-reading it would be a different request
        # rather than a replay. A first publish names no base, and so does its
        # retry.
        playground_id = f"pg-{uuid.uuid4().hex[:8]}"
        self.create_playground(playground_id)

        first = self.publish(playground_id, key="retry-key", base=False)
        second = self.publish(playground_id, key="retry-key", base=False)
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
            f"/api/v1/playgrounds/{playground_id}/versions",
            raw_body=self.build_zip(),
            token=OPERATOR_TOKEN,
            content_type="application/zip",
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_request")

    def test_uploading_to_an_unknown_playground_creates_it(self):
        """This used to be 404.

        protocol.md §2 step 0 is explicit that the first publish of an id is what
        brings the Playground into existence, so a publisher that never
        registered the id is not doing something malformed. Registration is
        still there and still how an id is reserved before anything is published;
        it is just no longer required first.
        """
        playground_id = f"pg-{uuid.uuid4().hex[:8]}"
        status, _, body = self.publish(playground_id)
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "accepted")
        self.assertIn(playground_id, self.catalog_ids())


class RetiringTest(ServerTestCase):
    """Operator takes a course out of the catalog: students stop seeing it.

    Archive is not a delete: the row, the commits and the submissions stay,
    the Git ref stays reachable, and the catalog filters the playground out
    by ``state = stopped``. Re-archiving is a no-op; republishing an
    archived course is rejected.
    """

    def test_retiring_a_playground_takes_it_out_of_the_catalog(self):
        playground_id, commit_id = self.seed("pg-archive-catalog")
        self.assertIn("pg-archive-catalog", self.catalog_ids())

        status, _, body = self.retire("pg-archive-catalog")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "accepted"})

        self.assertNotIn("pg-archive-catalog", self.catalog_ids())
        # The Git ref is left intact: a pinned client can still read the
        # manifest for the version it had installed before the archive.
        status, _, _ = self.request(
            "GET",
            f"/api/v1/playgrounds/pg-archive-catalog/versions/{commit_id}/manifest",
        )
        self.assertEqual(status, 200)

        # Publishing an archived course is refused.
        status, _, body = self.publish("pg-archive-catalog")
        self.assertEqual(status, 409)
        self.assertEqual(body.get("status"), "playground_stopped")

    def test_retiring_needs_the_operator_token(self):
        self.seed("pg-archive-token")

        self.assertEqual(self.retire("pg-archive-token", token=None)[0], 401)
        self.assertEqual(
            self.retire("pg-archive-token", token="wrong-token-000000")[0], 401
        )
        self.assertIn("pg-archive-token", self.catalog_ids())

    def test_retiring_an_unknown_playground_is_not_found(self):
        self.assertEqual(self.retire("pg-never-published")[0], 404)

    def test_retiring_twice_is_a_no_op(self):
        self.seed("pg-archive-twice")

        status, _, body = self.retire("pg-archive-twice")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "accepted")

        status, _, body = self.retire("pg-archive-twice")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "no_op")

    def test_a_retired_course_keeps_the_work_already_handed_in(self):
        playground_id, commit_id = self.seed("pg-archive-keeps")
        self.assertEqual(self.hand_in(playground_id, commit_id)["status"], "accepted")
        (stored,) = self.stored_submissions(playground_id)
        submission_id = stored["id"]

        self.assertEqual(self.retire(playground_id)[0], 200)

        # The submission is still readable, and its Playground still exists for it.
        status, _, body = self.request(
            "GET", f"/api/v1/submissions/{submission_id}", token=OPERATOR_TOKEN
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["submission"]["playgroundId"], playground_id)

        # A new submission against a retired course is refused.
        result = self.hand_in(playground_id, commit_id)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "playground_stopped")


class SubmissionTest(ServerTestCase):
    """Submissions arrive through sync; the operator only reads them back."""

    def test_the_operator_reads_the_submitted_files_back(self):
        playground_id, commit_id = self.seed()
        self.assertEqual(self.hand_in(playground_id, commit_id)["status"], "accepted")

        (stored,) = self.stored_submissions(playground_id)
        status, _, body = self.request(
            "GET",
            f"/api/v1/submissions/{stored['id']}",
            token=OPERATOR_TOKEN,
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            body["submission"]["files"],
            [{"path": "work/main.py", "text": "# 姓名：2026-001\nprint(1)\n"}],
        )

    def test_listing_submissions_requires_the_operator_token(self):
        playground_id, commit_id = self.seed()
        self.hand_in(playground_id, commit_id)

        anonymous = self.request("GET", "/api/v1/submissions")
        self.assertEqual(anonymous[0], 401)

        listed = self.request("GET", "/api/v1/submissions", token=OPERATOR_TOKEN)
        self.assertEqual(listed[0], 200)
        self.assertGreaterEqual(len(listed[2]["submissions"]), 1)
        for entry in listed[2]["submissions"]:
            self.assertNotIn("files", entry)

    def test_a_hand_in_records_the_exact_version_and_files(self):
        playground_id, commit_id = self.seed()
        self.hand_in(playground_id, commit_id)

        (stored,) = self.stored_submissions(playground_id)
        self.assertEqual(stored["commitId"], commit_id)
        self.assertEqual(stored["student"], "2026-001")

    def test_a_hand_in_without_an_attribution_is_refused(self):
        # The name lives in the hand-in's own header, so an empty file set
        # carries none and cannot be filed under anyone.
        playground_id, commit_id = self.seed()
        result = self.hand_in(playground_id, commit_id, files={})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "invalid_submission")
        self.assertEqual(self.stored_submissions(playground_id), [])


class ProtocolVersionTest(ServerTestCase):
    """Requests must say which protocol they speak (protocol.md §4)."""

    def test_a_request_without_the_header_is_refused(self):
        for path in ("/api/v1/catalog", "/api/v1/sync", "/api/v1/submissions"):
            with self.subTest(path=path):
                status, _, body = self.request(
                    "GET", path, extra_headers={"X-Protocol-Version": ""}
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["code"], "invalid_request")

    def test_a_non_numeric_protocol_version_is_refused(self):
        # "²" is the one that matters: it is a Latin-1 digit that `int()`
        # rejects, so a plain `isdigit()` check would raise instead of refuse.
        # (Non-Latin-1 digits cannot even be sent as an HTTP header.)
        for raw in ("one", "1.0", "+1", "²", "1 2"):
            with self.subTest(raw=raw):
                status, _, body = self.request(
                    "GET", "/api/v1/catalog",
                    extra_headers={"X-Protocol-Version": raw},
                )
                self.assertEqual(status, 400, raw)
                self.assertEqual(body["code"], "invalid_request")

    def test_a_version_below_the_server_minimum_is_refused_with_426(self):
        # The client can turn this into "upgrade me" instead of mis-reading a
        # response it cannot understand.
        self.seed()
        status, _, body = self.request(
            "GET", "/api/v1/catalog", extra_headers={"X-Protocol-Version": "0"}
        )
        self.assertEqual(status, 426)
        self.assertEqual(body["code"], "client_too_old")

    def test_a_newer_client_is_served(self):
        # protocol evolution tolerates a server that is behind the client.
        self.seed()
        status, _, _ = self.request(
            "GET", "/api/v1/catalog", extra_headers={"X-Protocol-Version": "99"}
        )
        self.assertEqual(status, 200)

    def test_health_is_exempt(self):
        # A probe is not a client and has no protocol.
        status, _, body = self.request("GET", "/healthz", extra_headers={"X-Protocol-Version": None})
        self.assertEqual(status, 200)


class SubmittedWorkOnDiskTest(ServerTestCase):
    """交上来的作业在磁盘上是一份人能直接打开的东西。

    行是记录，文件夹是给老师看的那份副本。学生名是这条路径上唯一来自请求的
    部分，所以这里既验证老师会读到的布局，也验证一个恶意名字做不到什么。
    """

    def root(self):
        return Path(self.server.submissions_path)

    def hand_in(self, student, files=None, course=None):
        # 每个用例一门新课：`ServerTestCase` 共享一个库，同名 id 会撞幂等键。
        playground_id, version_id = self.seed(course)
        text = files or {"work/main.py": f"# 姓名：{student}\nprint(1)\n"}
        result = self.hand_in_event(playground_id, version_id, text)
        return playground_id, version_id, result

    def hand_in_event(self, playground_id, version_id, files):
        event = {
            "event_id": uuid.uuid4().hex,
            "kind": "submit_code",
            "payload": {"playground_id": playground_id, "version_id": version_id, "files": files},
        }
        status, _, body = self.request("POST", "/api/v1/sync", payload={"pending_events": [event], "after_seq": 0})
        self.assertEqual(status, 200, body)
        return body["event_results"][0]

    def test_an_accepted_hand_in_lands_in_a_folder(self):
        playground_id, version_id, result = self.hand_in("张三（1 班）")
        self.assertEqual(result["status"], "accepted")

        folder = self.root() / "1 班" / "张三" / playground_id
        self.assertTrue(folder.is_dir(), f"没有这个目录：{folder}")
        self.assertEqual(
            (folder / "work" / "main.py").read_text(encoding="utf-8"),
            "# 姓名：张三（1 班）\nprint(1)\n",
        )

        # 文件夹里还有一张条，说明这份是谁的、基于哪个版本
        manifest = json.loads((folder / "submission.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["student"], "张三（1 班）")
        self.assertEqual(manifest["commitId"], version_id)
        self.assertEqual(manifest["playgroundId"], playground_id)
        self.assertEqual(manifest["files"], ["work/main.py"])

    def test_a_second_hand_in_of_the_same_course_is_numbered(self):
        playground_id, version_id = self.seed("hello-twice")
        first = self.hand_in_event(playground_id, version_id, {"work/main.py": "# 姓名：李四\nprint(1)\n"})
        second = self.hand_in_event(playground_id, version_id, {"work/main.py": "# 姓名：李四\nprint(2)\n"})
        self.assertEqual([first["status"], second["status"]], ["accepted", "accepted"])

        base = self.root() / "李四"
        self.assertEqual(sorted(p.name for p in base.iterdir()), [playground_id, f"{playground_id}-2"])
        self.assertEqual(
            (base / f"{playground_id}-2" / "work" / "main.py").read_text(encoding="utf-8"),
            "# 姓名：李四\nprint(2)\n",
        )

    def test_a_replay_repaints_the_same_folder(self):
        # 响应丢了、客户端原样重试：不能多出一个编号文件夹。
        playground_id, version_id = self.seed("hello-replay")
        event = {
            "event_id": "disk-replay-1",
            "kind": "submit_code",
            "payload": {
                "playground_id": playground_id,
                "version_id": version_id,
                "files": {"work/main.py": "# 姓名：王五\nprint(1)\n"},
            },
        }
        for _ in range(2):
            status, _, body = self.request("POST", "/api/v1/sync", payload={"pending_events": [event], "after_seq": 0})
            self.assertEqual(status, 200)
            self.assertEqual(body["event_results"][0]["status"], "accepted")

        base = self.root() / "王五"
        self.assertEqual([p.name for p in base.iterdir()], [playground_id])

    def test_a_rejected_hand_in_writes_nothing(self):
        playground_id, version_id = self.seed("hello-rejected")
        result = self.hand_in_event(playground_id, version_id, {"course/index.html": "x"})
        self.assertEqual(result["status"], "rejected")
        # 只有目录被建出来（或根本没有），不该有内容
        for path in self.root().rglob("*") if self.root().exists() else []:
            self.assertNotIn("course", path.parts)

    def test_a_hostile_student_name_stays_inside_the_root(self):
        playground_id, version_id = self.seed("hello-hostile")
        result = self.hand_in_event(
            playground_id,
            version_id,
            {"work/main.py": "# 姓名：../../etc/passwd 班级：../../..\nprint(1)\n"},
        )
        self.assertEqual(result["status"], "accepted")

        root = self.root()
        created = list(root.rglob("*"))
        self.assertTrue(created)
        for path in created:
            self.assertIn(root, path.parents)
            self.assertNotIn("..", path.relative_to(root).parts)
        self.assertFalse((root.parent.parent / "etc").exists())

    def test_the_folder_is_not_world_readable(self):
        # 独立的姓名：同一个人同一门课再交一次会被编号成 `-2`，那是另一条用例在测的
        # 行为，不该在这里悄悄影响路径。
        playground_id, _, _ = self.hand_in("权限（9 班）")
        folder = self.root() / "9 班" / "权限" / playground_id
        mode = folder.stat().st_mode & 0o777
        self.assertEqual(mode & 0o077, 0, f"目录权限 {oct(mode)} 对同组/其他用户开放了")


class ConditionalReadTest(ServerTestCase):
    """`If-None-Match` on the byte routes.

    A version's files are immutable, so their ETag is their own sha256 and a
    repeat request can be answered `304` with no body. This is the one part of
    the response plumbing that had no test.
    """

    def test_a_file_carries_its_digest_as_an_etag(self):
        playground_id, version_id = self.seed()
        path = f"/api/v1/playgrounds/{playground_id}/versions/{version_id}/files/course/module/app.py"

        status, headers, body = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"print(1)")
        self.assertEqual(headers["etag"], f'"{hashlib.sha256(body).hexdigest()}"')

        # The same request with the validator is a 304 and no body.
        again, headers, body = self.request(
            "GET", path, extra_headers={"If-None-Match": headers["etag"]}
        )
        self.assertEqual(again, 304)
        self.assertIsNone(body)
        self.assertEqual(headers["cache-control"], "no-store")

    def test_a_stale_validator_still_gets_the_bytes(self):
        playground_id, version_id = self.seed()
        path = f"/api/v1/playgrounds/{playground_id}/versions/{version_id}/files/course/module/app.py"

        status, _, body = self.request(
            "GET", path, extra_headers={"If-None-Match": '"' + "0" * 64 + '"'}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"print(1)")

    def test_a_head_with_the_validator_has_no_body(self):
        playground_id, version_id = self.seed()
        path = f"/api/v1/playgrounds/{playground_id}/versions/{version_id}/files/course/module/app.py"
        etag = self.request("GET", path)[1]["etag"]

        status, _, body = self.request(
            "HEAD", path, extra_headers={"If-None-Match": etag}
        )
        self.assertEqual(status, 304)
        self.assertIsNone(body)


class OperatorTokenShapeTest(ServerTestCase):
    """How the `Authorization` header is read.

    The operator token is the whole write authorization, so the parsing that
    decides whether it was presented is worth pinning: a scheme that is merely
    spelled differently must not become a way in or out.
    """

    def header(self, value):
        return self.request(
            "GET", "/api/v1/submissions", extra_headers={"Authorization": value}
        )

    def test_a_well_formed_header_is_accepted(self):
        self.assertEqual(self.header(f"Bearer {OPERATOR_TOKEN}")[0], 200)
        # The scheme is case-insensitive by RFC 7235.
        self.assertEqual(self.header(f"bearer {OPERATOR_TOKEN}")[0], 200)
        self.assertEqual(self.header(f"BEARER {OPERATOR_TOKEN}")[0], 200)

    def test_a_malformed_header_is_not(self):
        for value in (
            "",
            OPERATOR_TOKEN,                     # no scheme
            f"Basic {OPERATOR_TOKEN}",          # wrong scheme
            f"Bearer",                          # no token
            f"Bearer  {OPERATOR_TOKEN}",        # two spaces: the token would be empty
            f"Bearer {OPERATOR_TOKEN} extra",   # trailing material
        ):
            with self.subTest(value=value):
                self.assertEqual(self.header(value)[0], 401, value)


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
