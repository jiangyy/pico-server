"""Student projection over every Submission owned by one Principal."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for entry in (ROOT, SRC):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from picopg.server.app import PicoPGHttpServer  # noqa: E402
from picopg.server.auth.model import PrincipalSession  # noqa: E402
from picopg.server.drafts.protocol import revision_log_id  # noqa: E402
from picopg.server.submissions.logs import result_log  # noqa: E402
from picopg.replica import ReplicatedRecord, record_id_of  # noqa: E402
from picopg.replica.file_store import FileReplicaLogStore  # noqa: E402


def _record(log_id, parent, requires, data):
    return ReplicatedRecord(
        record_id_of(log_id, parent, requires, data), log_id, parent, requires, data
    )


def _submission(store, principal_id: str, replica_id: str, submission_id: str):
    revision_log = revision_log_id(principal_id, "lesson", replica_id)
    revision = _record(revision_log, None, (), {
        "type": "draft.revised",
        "draftId": "lesson",
        "replicaId": replica_id,
        "source": f"print({replica_id!r})\n",
        "base": None,
    })
    assert store.append(revision_log, None, (revision,)).kind == "accepted"

    request_log = f"principal/{principal_id}/submission/{replica_id}/requests"
    request = _record(request_log, None, (revision.position,), {
        "type": "submission.requested",
        "submissionId": submission_id,
        "playgroundId": "lesson",
        "revision": revision.position.to_value(),
    })
    assert store.append(request_log, None, (request,)).kind == "accepted"

    response_log = result_log(request_log)
    response = _record(response_log, None, (request.position, revision.position), {
        "type": "submission.completed",
        "submissionId": submission_id,
        "playgroundId": "lesson",
        "request": request.position.to_value(),
        "revision": revision.position.to_value(),
        "tag": f"picopg/submissions/{submission_id}",
    })
    assert store.append(response_log, None, (response,)).kind == "accepted"
    return request, revision


def _get(base_url: str, token: str) -> tuple[int, object]:
    request = urllib.request.Request(
        f"{base_url}/api/v1/submissions",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _invalid_submission(store, principal_id: str, replica_id: str):
    request_log = f"principal/{principal_id}/submission/{replica_id}/requests"
    request = _record(request_log, None, (), {"type": "not-a-submission"})
    assert store.append(request_log, None, (request,)).kind == "accepted"


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = FileReplicaLogStore(root / "replica")
        first, first_revision = _submission(store, "student", "A", "submission-a")
        _submission(store, "student", "B", "submission-b")
        _submission(store, "other", "C", "submission-c")
        _invalid_submission(store, "other", "D")
        _invalid_submission(store, "broken", "E")
        sessions = (
            PrincipalSession.create(
                "student", "A", "student-token", {}, ["submissions"]
            ),
            PrincipalSession.create(
                "broken", "E", "broken-token", {}, ["submissions"]
            ),
        )
        server = PicoPGHttpServer(
            ("127.0.0.1", 0),
            store,
            "operator-token",
            root / "packages",
            principal_sessions=sessions,
        )
        server.comments.add_submission(
            "student",
            "lesson",
            "teacher",
            "Only the first Submission.",
            "submission-a",
            first.position,
            first_revision.position,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        try:
            status, payload = _get(base_url, "student-token")
            assert status == 200
            submissions = payload["submissions"]  # type: ignore[index]
            assert [item["submissionId"] for item in submissions] == [
                "submission-a",
                "submission-b",
            ]
            assert submissions[0]["comments"] == [{
                "author": "teacher",
                "text": "Only the first Submission.",
            }]
            assert submissions[1]["comments"] == []
            assert all("replicaId" not in item for item in submissions)
            assert all(item["id"] for item in submissions)
            assert _get(base_url, "wrong")[0] == 401
            status, invalid = _get(base_url, "broken-token")
            assert status == 400
            assert invalid["error"]["code"] == "submission.invalid"  # type: ignore[index]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("student global Submission history and targeted comments passed")


if __name__ == "__main__":
    main()
