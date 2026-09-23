"""End-to-end contract test: the real client against the real server.

Every other test in this suite pins one side. This one pins the seam: a
running server is stood up in-process, and the client's own modules
(`apps/client/transport.ts`, `store.ts`, `sync.ts`, `submit.ts`) are run
against it by Node. Nothing in between is faked, so a field name, a path or a
header the two sides disagree about fails here.

The client lives in the other repository, so the test needs to be told where
it is:

    PICOPG_CLIENT_DIR=/path/to/learn-python/apps/client \\
        python3 -m unittest test_client_contract

Without it (or without `node` on PATH) the test is skipped rather than
guessing at a path.
"""

import json
import os
import shutil
import socket
import uuid
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

# Allow ``import server`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server as server_module  # noqa: E402
from server import make_server  # noqa: E402

HERE = Path(__file__).resolve().parent
DRIVER = HERE / "e2e_client.mjs"
OPERATOR_TOKEN = "e2e-operator-token-000"

# Where the client usually sits: either this server stands on its own with a
# `learn-python` checkout beside it, or it is vendored into that checkout as
# `learn-python/picopg/`. Both are tried, and an environment variable wins so a
# clone anywhere else still works.
DEFAULT_CLIENT_DIRS = (
    HERE.parents[2] / "learn-python" / "apps" / "client",
    HERE.parents[2] / "apps" / "client",
)


def client_dir():
    configured = os.environ.get("PICOPG_CLIENT_DIR")
    candidates = (Path(configured),) if configured else DEFAULT_CLIENT_DIRS
    for candidate in candidates:
        if (candidate / "transport.ts").exists():
            return candidate
    return None


CLIENT_DIR = client_dir()
NODE = shutil.which("node")


@unittest.skipUnless(
    CLIENT_DIR is not None and NODE is not None,
    "set PICOPG_CLIENT_DIR to the client's apps/client directory and install node",
)
class ClientContractTest(unittest.TestCase):
    """The client drives a real server through its own modules."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        base = Path(self.temporary_directory.name)
        self.server = make_server(
            host="127.0.0.1",
            port=0,
            database_path=base / "app.db",
            repository_path=base / "picopg.git",
            operator_token=OPERATOR_TOKEN,
        )
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.client_data = base / "client"

    # --- the operator's side -------------------------------------------------

    def operator(self, method, path, body=None, headers=None):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {OPERATOR_TOKEN}",
                "X-Protocol-Version": "1",
                **(headers or {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            # The body has to be read *and* closed: an unclosed error response
            # (the 409 from registering a course a second time, say) is a
            # ResourceWarning in the test output.
            with error:
                return error.code, json.loads(error.read() or b"null")

    def publish(self, playground_id, files=None, base=None):
        """Register and publish one course, returning its new version id.

        ``base`` is the Version this bundle was built on (``X-Base-Version-Id``);
        every publish after the first one has to name it.
        """
        files = files or {
            "picopg.json": json.dumps({"schemaVersion": 1, "runtime": "python"}),
            "course/index.html": "<html>hello</html>",
        }
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for path, text in files.items():
                archive.writestr(path, text)

        self.operator(
            "POST",
            "/api/v1/playgrounds",
            json.dumps({"id": playground_id}).encode(),
            {"Content-Type": "application/json"},
        )
        headers = {
            "Content-Type": "application/zip",
            # A fresh key per publish: the key identifies one publish, and a
            # second publish of the same course is a different request.
            "Idempotency-Key": uuid.uuid4().hex,
        }
        if base is not None:
            headers["X-Base-Version-Id"] = base
        status, body = self.operator(
            "POST",
            f"/api/v1/playgrounds/{playground_id}/versions",
            buffer.getvalue(),
            headers,
        )
        self.assertIn(status, (200, 201), body)
        return body["version_id"]

    # --- the client's side ---------------------------------------------------

    def client(self, step, *args, data=None):
        """One driver step. `data` picks which machine's local store it runs on."""

        result = subprocess.run(
            [NODE, str(DRIVER), str(self.port), str(CLIENT_DIR), str(data or self.client_data), step, *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout.strip()
        self.assertNotIn("FAILED", output, f"{output}\n{result.stderr}")
        self.assertEqual(result.returncode, 0, f"{output}\n{result.stderr}")
        line = next(line for line in output.splitlines() if line.startswith("RESULT "))
        return json.loads(line[len("RESULT ") :])

    # --- the seam ------------------------------------------------------------

    def test_the_client_can_bootstrap_install_submit_and_retire(self):
        version_id = self.publish("pg-e2e")

        # 1. First launch: read the catalog and install its content.
        boot = self.client("bootstrap")
        self.assertEqual(
            boot["installed"],
            [{"playgroundId": "pg-e2e", "versionId": version_id}],
        )
        self.assertEqual(boot["active"], ["pg-e2e"])
        self.assertEqual(boot["problems"], [])

        # 2. The content really is on disk, readable through the client's own
        #    store, and the bytes are the ones the server published.
        manifest = self.read_local_manifest("pg-e2e", version_id)
        self.assertEqual(
            sorted(entry["path"] for entry in manifest["files"]),
            ["course/index.html", "picopg.json"],
        )
        self.assertEqual(self.read_local_file(manifest, "course/index.html"), "<html>hello</html>")

        # 3. A hand-in travels as one named delivery and comes back stored.
        hand_in = self.client("submit", "pg-e2e", version_id, "张三")
        self.assertEqual(hand_in["outcome"], "stored")
        self.assertEqual(hand_in["pending"], 0)
        # 一次投递一次请求：客户端不是把队列攒成一批发出去的。
        self.assertEqual(hand_in["http"]["submissions"], 1)

        # 4. The server stored it under the name the file carries.
        (stored,) = self.stored_submissions("pg-e2e")
        self.assertEqual(stored["student"], "张三")
        self.assertEqual(stored["commitId"], version_id)

        # 5. Archiving reaches the client as the course disappearing from the
        #    catalog, and the next round retires it without deleting bytes.
        status, _ = self.operator("DELETE", "/api/v1/playgrounds/pg-e2e")
        self.assertEqual(status, 200)
        after = self.client("sync")
        self.assertEqual(after["retired"], ["pg-e2e"])
        self.assertEqual(after["active"], [])

    def test_a_repair_bootstrap_matches_the_catalog_snapshot(self):
        """The one bootstrap that has something to reconcile.

        A brand-new client has no local set to disagree with the snapshot, but a
        *repaired* install does: `stateAfterStartup` re-runs bootstrap when the
        local content is damaged, and the rebuilt set has to be exactly what the
        snapshot names — anything the server dropped in the meantime is out, and
        nothing else may survive.
        """
        self.publish("pg-e2e")
        self.publish("pg-other")
        self.client("bootstrap")

        # The server drops one of them while this machine is not looking.
        status, _ = self.operator("DELETE", "/api/v1/playgrounds/pg-other")
        self.assertEqual(status, 200)

        # Rebuilding the install (a damaged blob, a lost cursor) must land on
        # the catalog exactly: what the snapshot names, and nothing else.
        boot = self.client("bootstrap")
        self.assertEqual(boot["active"], ["pg-e2e"])
        self.assertEqual(boot["problems"], [])

        # And the retired course really is gone from the local set, not merely
        # absent from this response.
        after = self.client("sync")
        self.assertEqual(after["active"], ["pg-e2e"])

    def test_a_restored_course_comes_back_to_the_client(self):
        """Restore reaches a client as an ordinary publish.

        There is no "restored" kind: the server emits ``publish_playground`` for
        the Version it puts back, which is a shape every client already handles
        (install that Version, put it back in the active set). A new kind would
        be dropped as unknown by §4 and the course would never return.
        """
        version_id = self.publish("pg-e2e")
        self.client("bootstrap")

        status, _ = self.operator("DELETE", "/api/v1/playgrounds/pg-e2e")
        self.assertEqual(status, 200)
        self.assertEqual(self.client("sync")["active"], [])

        status, body = self.operator("POST", "/api/v1/playgrounds/pg-e2e/restore")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"status": "accepted"})

        after = self.client("sync")
        self.assertEqual(after["installed"], ["pg-e2e"], "恢复必须真的把它装回来")
        self.assertEqual(after["active"], ["pg-e2e"])
        self.assertEqual(after["stored"], [], "恢复不产生交付结果")
        # 内容就是原来那个 Version，不是新造的
        manifest = self.read_local_manifest("pg-e2e", version_id)
        self.assertEqual(self.read_local_file(manifest, "course/index.html"), "<html>hello</html>")

        # 幂等：再来一次是 no_op，客户端也不需要再装一次
        self.assertEqual(
            self.operator("POST", "/api/v1/playgrounds/pg-e2e/restore")[1],
            {"status": "no_op"},
        )
        self.assertEqual(self.client("sync")["installed"], [])

    def test_a_hand_in_written_offline_is_still_accepted_after_the_course_moves_on(self):
        """断网时写好的作业，等联网时课程已经发了新版本 —— 这份作业仍然收。

        picopg-implementation.md §9 只要求 ``version_id`` 属于该 Playground，
        实现里就是「在该课程已发布的 history 里」。而 history 不会被删（Archive
        只改 state），所以一份钉在旧版本上、隔了一段时间才送到的作业仍然有效，
        并且被记在它真正依据的那个版本上 —— 这一点很重要：学生是按那个版本做的。
        """
        first = self.publish("pg-offline")
        self.client("bootstrap")

        # 断网：驱动指向一个没有监听的端口，所以本地入队成功、上传失败。
        self.write_hand_in("# 姓名：王五\nprint(3)\n")
        offline = self.client_on(self.dead_port(), "submit", "pg-offline", first, "王五")
        self.assertEqual(offline["pending"], 1, "断网时作业必须留在队列里")

        # 网络不在的这段时间，课程往前走了一个版本
        second = self.publish(
            "pg-offline",
            files={
                "picopg.json": json.dumps({"schemaVersion": 1, "runtime": "python"}),
                "course/index.html": "<html>second</html>",
            },
            base=first,
        )
        self.assertNotEqual(second, first)

        # 联网后一次常规同步：先把作业送上去，再把新版本拉下来
        after = self.client("sync")
        self.assertEqual(len(after["stored"]), 1, after)
        self.assertEqual(after["refused"], [])
        self.assertEqual(after["undelivered"], [])
        self.assertEqual(after["pending"], 0)
        self.assertEqual(after["installed"], ["pg-offline"], "新版本要自动拉下来")
        self.assertEqual(after["retired"], [])

        (stored,) = self.stored_submissions("pg-offline")
        self.assertEqual(stored["student"], "王五")
        self.assertEqual(stored["commitId"], first, "记在它依据的那个版本上，不是当前版本")

        # 而本地现在钉在新版本上
        manifest = self.read_local_manifest("pg-offline", second)
        self.assertEqual(self.read_local_file(manifest, "course/index.html"), "<html>second</html>")

    def test_each_queued_hand_in_is_one_request_and_one_answer(self):
        """A queue of hand-ins drains one delivery at a time.

        There is no batch and no round cap any more: each hand-in is its own
        request, named by the client, and the server answers each one on its
        own. What this pins is that a queue of several hand-ins produces one
        request each — the client never catches a refusal it cannot attribute to
        a delivery, which is what the old whole-round cap made impossible.
        """
        version_id = self.publish("pg-queue")
        self.client("bootstrap")

        self.write_hand_in("# 姓名：甲\nprint(1)\n")
        queued_offline = self.client_on(
            self.dead_port(), "submit", "pg-queue", version_id, "甲"
        )
        self.assertEqual(queued_offline["pending"], 1)
        self.write_hand_in("# 姓名：乙\nprint(2)\n")
        self.client_on(self.dead_port(), "submit", "pg-queue", version_id, "乙")
        self.write_hand_in("# 姓名：丙\nprint(3)\n")
        self.client_on(self.dead_port(), "submit", "pg-queue", version_id, "丙")
        self.assertEqual(self.pending_count(), 3)

        after = self.client("sync")
        self.assertEqual(len(after["stored"]), 3, after)
        self.assertEqual(after["refused"], [])
        self.assertEqual(after["undelivered"], [])
        self.assertEqual(after["pending"], 0)
        self.assertEqual(after["http"]["submissions"], 3, "三条待送 = 三次请求")
        self.assertEqual(after["http"]["catalog"], 1, "目录一轮读一次")
        self.assertEqual(
            sorted(entry["student"] for entry in self.stored_submissions("pg-queue")),
            ["丙", "乙", "甲"],
        )

    def test_two_machines_that_wrote_offline_do_not_fork(self):
        """两台电脑各自脱网写作业，各自联网 —— 两份都在，谁也没有分叉。

        The timeline is the server's, and that is what makes this work:

          * A hand-in is a *fact*, not a slot to claim. Each machine invents its
            own `submission_id` before it ever has a network, so two machines
            writing at the same time produce two submissions, both stored. There
            is no "last write wins" and nothing is overwritten.
          * Content cannot fork at all: only the operator publishes, and a
            publish is a compare-and-swap on `base_version_id`.
          * Neither machine's *view* diverges either: both read the same catalog
            and end up with the same installed set and the same current Version,
            without any reconciliation.

        A hand-in written offline is accepted even though the course moved on in
        the meantime: picopg-implementation.md §3 only asks that the Version belong to that
        Playground's published history.
        """
        first = self.publish("pg-two")
        mine = self.client_data
        theirs = self.client_data.parent / "client-b"
        self.client("bootstrap", data=mine)
        self.client("bootstrap", data=theirs)

        # 两台都断网，各写一份不同的（同一门课、同一版本）
        self.write_hand_in("# 姓名：甲\nprint(1)\n", data=mine)
        self.write_hand_in("# 姓名：乙\nprint(2)\n", data=theirs)
        for machine, who in ((mine, "甲"), (theirs, "乙")):
            offline = self.client_on(self.dead_port(), "submit", "pg-two", first, who, data=machine)
            self.assertEqual(offline["pending"], 1, f"{who} 的作业必须留在自己队列里")

        # 网络不在的这段时间，课程往前走了一版
        second = self.publish(
            "pg-two",
            files={
                "picopg.json": json.dumps({"schemaVersion": 1, "runtime": "python"}),
                "course/index.html": "<html>second</html>",
            },
            base=first,
        )
        self.assertNotEqual(second, first)

        # 各自联网：各送各的作业，各拉同一版
        for machine, who in ((mine, "甲"), (theirs, "乙")):
            after = self.client("sync", data=machine)
            self.assertEqual(len(after["stored"]), 1, f"{who}: {after}")
            self.assertEqual(after["refused"], [], who)
            self.assertEqual(after["pending"], 0, who)
            self.assertEqual(after["installed"], ["pg-two"], f"{who} 要把新版拉下来")

        # 服务端：两份都在，各自记在自己依据的那个版本上
        stored = self.stored_submissions("pg-two")
        self.assertEqual(sorted(entry["student"] for entry in stored), ["乙", "甲"])
        self.assertEqual(
            {entry["commitId"] for entry in stored},
            {first},
            "两份都是按断网时那一版写的，不是当前版本",
        )

        # 两台最后站在同一个位置上（同一份已装集合、同一个当前版本）
        positions = [self.client("sync", data=machine) for machine in (mine, theirs)]
        self.assertEqual(
            {tuple(sorted(position["active"])) for position in positions},
            {tuple(sorted(positions[0]["active"]))},
        )
        self.assertEqual(
            [entry["installed"] for entry in positions],
            [positions[0]["installed"]] * len(positions),
            "两台都对着同一份目录，装出来的东西一样",
        )
        self.assertEqual(self.pending_count(data=mine), 0)
        self.assertEqual(self.pending_count(data=theirs), 0)

    def test_a_later_round_installs_only_what_changed(self):
        self.publish("pg-e2e")
        first = self.client("bootstrap")
        self.assertEqual(first["active"], ["pg-e2e"])
        self.assertEqual(first["http"]["manifests"], 1)

        second = self.publish("pg-other")
        # The same machine: the catalog is read again, but only the new course
        # costs a manifest and a file — the installed one is compared by version
        # id and left alone.
        after = self.client("sync")
        self.assertEqual(after["installed"], ["pg-other"])
        self.assertEqual(sorted(after["active"]), ["pg-e2e", "pg-other"])
        self.assertEqual(after["http"]["catalog"], 1)
        self.assertEqual(after["http"]["manifests"], 1, "已经装好的版本不该再拉一遍")
        # 新课的页面内容和旧课逐字节相同，而内容是按 sha256 存的：这一份已经在
        # 磁盘上，所以连文件都不用下载。
        self.assertEqual(after["http"]["files"], 0, "同样的内容只下过一次")
        self.assertTrue(second)

    def test_an_offline_hand_in_survives_a_restart_and_is_sent_once(self):
        version_id = self.publish("pg-e2e")
        self.client("bootstrap")

        # Queue a hand-in while the server is unreachable, then restart the
        # process: the queued delivery must still be there.
        self.server.shutdown()
        self.server.server_close()
        self.write_hand_in("# 姓名：李四\nprint(2)\n")
        queued = self.run_driver_expecting_failure("submit", "pg-e2e", version_id, "李四")
        self.assertEqual(queued["pending"], 1)

        # A fresh server on the same data directory: the client retries the
        # queued delivery with its original id, so the round produces exactly
        # one submission — and the name is the one the file already carried.
        self.restart_server()
        retry = self.client("sync")
        self.assertEqual(len(retry["stored"]), 1)
        self.assertEqual(retry["pending"], 0)
        (stored,) = self.stored_submissions("pg-e2e")
        self.assertEqual(stored["student"], "李四")
        self.assertEqual(stored["commitId"], version_id)

    # --- helpers -------------------------------------------------------------

    def dead_port(self):
        """A port nothing is listening on: the machine this client runs on has
        no route to the server. Cheaper and more honest than stopping the
        server, which the test needs up to publish the next Version."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def client_on(self, port, step, *args, data=None):
        """Run one driver step against another port, expecting it to fail."""
        subprocess.run(
            [NODE, str(DRIVER), str(port), str(CLIENT_DIR), str(data or self.client_data), step, *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {"pending": self.pending_count(data=data)}

    def run_driver_expecting_failure(self, step, *args):
        subprocess.run(
            [NODE, str(DRIVER), str(self.port), str(CLIENT_DIR), str(self.client_data), step, *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
        # The round failed, so read the queue straight from the client's store.
        return {"pending": self.pending_count()}

    def write_hand_in(self, text, data=None):
        machine = data or self.client_data
        machine.mkdir(parents=True, exist_ok=True)
        (machine / "hand-in.py").write_text(text, encoding="utf-8")

    def pending_count(self, data=None):
        raw = json.loads(((data or self.client_data) / "pending.json").read_text(encoding="utf-8"))
        return len(raw["submissions"])

    def restart_server(self):
        base = Path(self.temporary_directory.name)
        self.server = make_server(
            host="127.0.0.1",
            port=self.port,
            database_path=base / "app.db",
            repository_path=base / "picopg.git",
            operator_token=OPERATOR_TOKEN,
        )
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def stored_submissions(self, playground_id):
        status, body = self.operator("GET", "/api/v1/submissions")
        self.assertEqual(status, 200)
        return [
            entry
            for entry in body["submissions"]
            if entry["playgroundId"] == playground_id
        ]

    def read_local_manifest(self, playground_id, version_id):
        # The store names its files by a hash of the ids, so go through the
        # client's own reader rather than guessing a path.
        script = (
            "const [dir, pg, vid] = process.argv.slice(1);"
            f"const store = await import({str(CLIENT_DIR / 'store.ts')!r});"
            "const manifest = await store.readManifest(dir, pg, vid);"
            "console.log(JSON.stringify(manifest));"
        )
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script, str(self.client_data), playground_id, version_id],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def read_local_file(self, manifest, path):
        entry = next(file for file in manifest["files"] if file["path"] == path)
        script = (
            "const [dir, sha] = process.argv.slice(1);"
            f"const store = await import({str(CLIENT_DIR / 'store.ts')!r});"
            "const bytes = await store.readBlob(dir, sha);"
            "console.log(Buffer.from(bytes).toString('utf8'));"
        )
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script, str(self.client_data), entry["sha256"]],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main(verbosity=2)
