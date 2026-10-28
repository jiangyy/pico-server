


"""HTTP contract test for the operator Submission workbench projection."""

from __future__ import annotations

import unittest
import json
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


from picopg.server.app import PicoPGHttpServer
from picopg.server.drafts.protocol import revision_log_id
from picopg.server.submissions.logs import result_log as submission_result_log
from picopg.replica import ReplicatedRecord, record_id_of
from picopg.replica.file_store import FileReplicaLogStore


def _record(log_id, parent, requires, data):
    return ReplicatedRecord(
        record_id_of(log_id, parent, requires, data), log_id, parent, requires, data
    )


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: object | None = None,
    token: str = "operator-token",
) -> tuple[int, object]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base_url + path, data=payload, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read() or b"null")


def _seed(store: FileReplicaLogStore) -> ReplicatedRecord:
    revision_log = revision_log_id("student", "lesson", "device")
    revision = _record(revision_log, None, (), {
        "type": "draft.revised",
        "draftId": "lesson",
        "replicaId": "device",
        "source": "print('exact revision')\n",
        "base": None,
    })
    assert store.append(revision_log, None, (revision,)).kind == "accepted"

    request_log = "principal/student/submission/device/requests"
    submission = _record(request_log, None, (revision.position,), {
        "type": "submission.requested",
        "submissionId": "submission-1",
        "playgroundId": "lesson",
        "revision": revision.position.to_value(),
    })
    assert store.append(request_log, None, (submission,)).kind == "accepted"

    result_log = submission_result_log(request_log)
    result = _record(result_log, None, (submission.position, revision.position), {
        "type": "submission.completed",
        "submissionId": "submission-1",
        "playgroundId": "lesson",
        "request": submission.position.to_value(),
        "revision": revision.position.to_value(),
        "tag": "picopg/submissions/example",
    })
    assert store.append(result_log, None, (result,)).kind == "accepted"
    return submission


class SubmissionsTest(unittest.TestCase):
    def test_submissions(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            store = FileReplicaLogStore(root / "replica")
            submission = _seed(store)
            server = PicoPGHttpServer(
                ("127.0.0.1", 0), store, "operator-token", root / "packages"
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            request_id = quote(submission.id, safe="")
            try:
                status, payload = _request(base_url, "/api/v1/operator/submissions")
                assert status == 200
                listed = payload["submissions"]  # type: ignore[index]
                assert len(listed) == 1
                assert listed[0]["status"] == "completed"
                assert listed[0]["requestRecordId"] == submission.id

                status, detail = _request(
                    base_url, f"/api/v1/operator/submissions/{request_id}"
                )
                assert status == 200
                assert detail["source"] == "print('exact revision')\n"  # type: ignore[index]
                assert detail["comments"] == []  # type: ignore[index]

                status, _ = _request(
                    base_url,
                    f"/api/v1/operator/submissions/{request_id}/comments",
                    method="POST",
                    body={"author": "teacher", "text": "Keep this exact result."},
                )
                assert status == 201

                status, detail = _request(
                    base_url, f"/api/v1/operator/submissions/{request_id}"
                )
                assert status == 200
                assert detail["comments"] == [{  # type: ignore[index]
                    "position": detail["comments"][0]["position"],  # type: ignore[index]
                    "author": "teacher",
                    "text": "Keep this exact result.",
                }]

                status, _ = _request(
                    base_url, "/api/v1/operator/submissions", token="wrong"
                )
                assert status == 401
                status, _ = _request(
                    base_url, "/api/v1/operator/submissions/missing"
                )
                assert status == 404
                status, invalid = _request(
                    base_url,
                    f"/api/v1/operator/submissions/{request_id}/comments",
                    method="POST",
                    body={"author": "teacher", "text": "   "},
                )
                assert status == 400
                assert invalid["error"]["code"] == "submission.invalid"  # type: ignore[index]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
