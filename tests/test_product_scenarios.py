


"""Product scenario: two replicas, offline work, takeover, releases and submissions."""

from __future__ import annotations

import unittest
import io
import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import quote


from picopg.server.app import PicoPGHttpServer
from picopg.server.auth.model import PrincipalSession
from picopg.server.drafts.protocol import (
    decision_log_id,
    request_log_id,
    revision_log_id,
)
from picopg.server.playgrounds.releases import PLAYGROUND_RELEASES_LOG_ID
from picopg.server.submissions.logs import result_log
from picopg.replica import Position, ReplicatedRecord, record_id_of
from picopg.replica.file_store import FileReplicaLogStore


class Tags:
    def __init__(self) -> None:
        self.sources: dict[str, str] = {}

    def create(self, *, idempotency_key: str, submission_id: str,
               playground_id: str, revision: Position, source: str) -> str:
        self.sources[submission_id] = source
        return f"picopg/submissions/{submission_id}"


def _package(title: str, source: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("picopg.json", json.dumps({
            "schemaVersion": 1,
            "runtime": "python",
            "module": "course/demo.js",
            "title": title,
        }))
        archive.writestr("course/index.html", "course")
        archive.writestr("course/demo.js", "export const starter = ''; export const Root = () => null;")
        archive.writestr("starter/main.py", source)
    return output.getvalue()


def _record(log_id: str, parent: Position | None, requires: tuple[Position, ...],
            data: dict[str, object]) -> ReplicatedRecord:
    return ReplicatedRecord(
        record_id_of(log_id, parent, requires, data),
        log_id,
        parent,
        requires,
        data,
    )


def _submission_request_log(replica_id: str) -> str:
    return f"principal/student/submission/{replica_id}/requests"


def _request(base_url: str, path: str, token: str, *, method: str = "GET",
             value: object | None = None) -> tuple[int, object]:
    body = None if value is None else json.dumps(value, separators=(",", ":")).encode()
    request = urllib.request.Request(
        base_url + path,
        method=method,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            raw = response.read()
            return response.status, None if not raw else json.loads(raw)
    except urllib.error.HTTPError as error:
        with error:
            raw = error.read()
            return error.code, None if not raw else json.loads(raw)


def _append(base_url: str, token: str, record: ReplicatedRecord) -> None:
    status, value = _request(
        base_url,
        f"/api/v1/replica/logs/{quote(record.log_id, safe='')}/records",
        token,
        method="POST",
        value={
            "expectedHead": None if record.parent is None else record.parent.to_value(),
            "records": [record.to_value()],
        },
    )
    assert status == 200, (status, value)
    assert value["kind"] == "accepted"  # type: ignore[index]


def _records(base_url: str, token: str, log_id: str) -> list[dict[str, object]]:
    status, value = _request(
        base_url,
        f"/api/v1/replica/logs/{quote(log_id, safe='')}/records?limit=128",
        token,
    )
    assert status == 200, (status, value)
    return value["records"]  # type: ignore[index,return-value]


def _wait(condition, message: str) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(message)


class ProductScenariosTest(unittest.TestCase):
    def test_product_scenarios(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileReplicaLogStore(root / "replica")
            tags = Tags()
            sessions = (
                PrincipalSession.create(
                    "student", "A", "token-a", {}, ["playgrounds", "drafts", "submissions", "analysis"]
                ),
                PrincipalSession.create(
                    "student", "B", "token-b", {}, ["playgrounds", "drafts", "submissions", "analysis"]
                ),
            )
            server = PicoPGHttpServer(
                ("127.0.0.1", 0),
                store,
                "operator-token",
                root / "packages",
                principal_sessions=sessions,
                submission_tag_writer=tags,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            try:
                first = _package("Lesson v1", "print('starter v1')\n")
                published = server.playgrounds.publish("lesson", None, "application/zip", first)
                assert published.status == "accepted"
                assert len(_records(base_url, "token-a", PLAYGROUND_RELEASES_LOG_ID)) == 1

                revision_a_log = revision_log_id("student", "lesson", "A")
                revision_b_log = revision_log_id("student", "lesson", "B")
                request_a_log = request_log_id("student", "lesson", "A")
                request_b_log = request_log_id("student", "lesson", "B")
                decisions_log = decision_log_id("student", "lesson")

                revision_a1 = _record(revision_a_log, None, (), {
                    "type": "draft.revised", "draftId": "lesson", "replicaId": "A",
                    "source": "print('A1')\n", "base": None,
                })
                _append(base_url, "token-a", revision_a1)
                begin_a = _record(request_a_log, None, (revision_a1.position,), {
                    "type": "draft.beginEditing", "commandId": "begin-a",
                    "draftId": "lesson", "replicaId": "A",
                    "expectedHead": revision_a1.position.to_value(), "expectedEpoch": 0,
                })
                _append(base_url, "token-a", begin_a)
                _wait(
                    lambda: any(item["data"]["type"] == "draft.editingStarted"
                                for item in _records(base_url, "token-b", decisions_log)),
                    "A did not acquire the Draft",
                )

                revision_b1 = _record(revision_b_log, None, (revision_a1.position,), {
                    "type": "draft.revised", "draftId": "lesson", "replicaId": "B",
                    "source": "print('B offline')\n", "base": revision_a1.position.to_value(),
                })
                _append(base_url, "token-b", revision_b1)
                begin_b = _record(request_b_log, None, (revision_a1.position,), {
                    "type": "draft.beginEditing", "commandId": "begin-b",
                    "draftId": "lesson", "replicaId": "B",
                    "expectedHead": revision_a1.position.to_value(), "expectedEpoch": 1,
                })
                _append(base_url, "token-b", begin_b)
                _wait(
                    lambda: any(item["data"].get("commandId") == "begin-b"
                                and item["data"]["type"] == "draft.rejected"
                                for item in _records(base_url, "token-b", decisions_log)),
                    "B's competing begin was not rejected",
                )
                takeover_b = _record(request_b_log, begin_b.position,
                                     (revision_a1.position, revision_b1.position), {
                    "type": "draft.takeover", "commandId": "takeover-b",
                    "draftId": "lesson", "replicaId": "B",
                    "expectedHead": revision_a1.position.to_value(), "expectedEpoch": 1,
                    "targetHead": revision_b1.position.to_value(),
                })
                _append(base_url, "token-b", takeover_b)
                _wait(
                    lambda: any(item["data"].get("commandId") == "takeover-b"
                                and item["data"]["type"] == "draft.takenOver"
                                for item in _records(base_url, "token-a", decisions_log)),
                    "B did not take over the Draft",
                )

                revision_a2 = _record(revision_a_log, revision_a1.position, (), {
                    "type": "draft.revised", "draftId": "lesson", "replicaId": "A",
                    "source": "print('A offline')\n", "base": revision_a1.position.to_value(),
                })
                _append(base_url, "token-a", revision_a2)
                takeover_a = _record(request_a_log, begin_a.position,
                                     (revision_b1.position, revision_a2.position), {
                    "type": "draft.takeover", "commandId": "takeover-a",
                    "draftId": "lesson", "replicaId": "A",
                    "expectedHead": revision_b1.position.to_value(), "expectedEpoch": 2,
                    "targetHead": revision_a2.position.to_value(),
                })
                _append(base_url, "token-a", takeover_a)
                _wait(
                    lambda: any(item["data"].get("commandId") == "takeover-a"
                                and item["data"]["type"] == "draft.takenOver"
                                and item["data"]["writer"] == "A"
                                for item in _records(base_url, "token-b", decisions_log)),
                    "A's offline branch did not become canonical after explicit takeover",
                )

                second = _package("Lesson v2", "print('starter v2')\n")
                republished = server.playgrounds.publish(
                    "lesson", published.version_id, "application/zip", second
                )
                assert republished.status == "accepted"
                assert len(_records(base_url, "token-b", PLAYGROUND_RELEASES_LOG_ID)) == 2

                submission_a_log = _submission_request_log("A")
                submission_b_log = _submission_request_log("B")
                submission_a = _record(submission_a_log, None, (revision_a2.position,), {
                    "type": "submission.requested", "submissionId": "from-a",
                    "playgroundId": "lesson", "revision": revision_a2.position.to_value(),
                })
                submission_b = _record(submission_b_log, None, (revision_b1.position,), {
                    "type": "submission.requested", "submissionId": "from-b",
                    "playgroundId": "lesson", "revision": revision_b1.position.to_value(),
                })
                _append(base_url, "token-a", submission_a)
                _append(base_url, "token-b", submission_b)
                _wait(
                    lambda: len(_records(base_url, "token-a", result_log(submission_a_log))) == 1
                    and len(_records(base_url, "token-b", result_log(submission_b_log))) == 1,
                    "Submission workers did not complete both device requests",
                )
                _wait(
                    lambda: len(_request(base_url, "/api/v1/submissions", "token-b")[1]["submissions"]) == 2,
                    "Principal-wide Submission history did not include both devices",
                )
                status, value = _request(base_url, "/api/v1/submissions", "token-b")
                assert status == 200
                submissions = value["submissions"]  # type: ignore[index]
                assert {item["submissionId"] for item in submissions} == {"from-a", "from-b"}
                assert all("replicaId" not in item for item in submissions)
                assert tags.sources == {
                    "from-a": "print('A offline')\n",
                    "from-b": "print('B offline')\n",
                }
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
