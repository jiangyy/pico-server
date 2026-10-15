"""Class roster authority and teacher Submission access."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for entry in (ROOT, SRC):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from apps.server.application import PicoPGHttpServer  # noqa: E402
from apps.server.authentication.model import PrincipalSession  # noqa: E402
from apps.server.draft.protocol import revision_log_id  # noqa: E402
from picopg.api import ReplicatedRecord, record_id_of  # noqa: E402
from picopg.impl.file_replica_log import FileReplicaLogStore  # noqa: E402


def _record(log_id, parent, requires, data):
    return ReplicatedRecord(
        record_id_of(log_id, parent, requires, data), log_id, parent, requires, data
    )


def _submission(store, principal_id: str, replica_id: str) -> ReplicatedRecord:
    revision_log = revision_log_id(principal_id, "lesson", replica_id)
    revision = _record(revision_log, None, (), {
        "type": "draft.revised",
        "draftId": "lesson",
        "replicaId": replica_id,
        "source": f"print({principal_id!r})\n",
        "base": None,
    })
    assert store.append(revision_log, None, (revision,)).kind == "accepted"
    request_log = f"principal/{principal_id}/submission/{replica_id}/requests"
    request = _record(request_log, None, (revision.position,), {
        "type": "submission.requested",
        "submissionId": f"submission-{principal_id}",
        "playgroundId": "lesson",
        "revision": revision.position.to_value(),
    })
    assert store.append(request_log, None, (request,)).kind == "accepted"
    return request


def _request(
    base_url: str,
    path: str,
    token: str,
    *,
    method: str = "GET",
    value: object | None = None,
) -> tuple[int, object]:
    body = None if value is None else json.dumps(value).encode()
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
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = FileReplicaLogStore(root / "replica")
        s1 = _submission(store, "s1", "s1-device")
        _submission(store, "s2", "s2-device")
        s3 = _submission(store, "s3", "s3-device")
        sessions = tuple(
            PrincipalSession.create(principal, "device", f"{principal}-token", {}, ())
            for principal in ("t1", "t2", "s1", "s2", "s3")
        )
        server = PicoPGHttpServer(
            ("127.0.0.1", 0),
            store,
            "operator-token",
            root / "packages",
            principal_sessions=sessions,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        try:
            status, _ = _request(
                base_url,
                "/api/v1/operator/classes/python-1",
                "operator-token",
                method="POST",
                value={"teachers": ["t1"], "students": ["s2", "s1"]},
            )
            assert status == 201
            directory = server.class_rosters.directory()
            assert directory.students_of("t1") == frozenset(("s1", "s2"))

            status, listed = _request(
                base_url, "/api/v1/teacher/submissions", "t1-token"
            )
            assert status == 200
            assert {item["principalId"] for item in listed["submissions"]} == {"s1", "s2"}
            assert _request(
                base_url, "/api/v1/teacher/submissions", "t2-token"
            ) == (200, {"submissions": []})

            s1_id = quote(s1.id, safe="")
            s3_id = quote(s3.id, safe="")
            status, detail = _request(
                base_url, f"/api/v1/teacher/submissions/{s1_id}", "t1-token"
            )
            assert status == 200
            assert detail["source"] == "print('s1')\n"
            assert _request(
                base_url, f"/api/v1/teacher/submissions/{s3_id}", "t1-token"
            )[0] == 404

            status, _ = _request(
                base_url,
                f"/api/v1/teacher/submissions/{s1_id}/comments",
                "t1-token",
                method="POST",
                value={"text": "Good."},
            )
            assert status == 201
            assert _request(
                base_url,
                f"/api/v1/teacher/submissions/{s1_id}/comments",
                "t1-token",
                method="POST",
                value={"author": "forged", "text": "No."},
            )[0] == 400
            _, detail = _request(
                base_url, f"/api/v1/teacher/submissions/{s1_id}", "t1-token"
            )
            assert detail["comments"][0]["author"] == "t1"

            status, _ = _request(
                base_url,
                "/api/v1/operator/classes/python-1",
                "operator-token",
                method="POST",
                value={"teachers": ["t1"], "students": ["s3"]},
            )
            assert status == 201
            assert _request(
                base_url, f"/api/v1/teacher/submissions/{s1_id}", "t1-token"
            )[0] == 404
            assert _request(
                base_url, f"/api/v1/teacher/submissions/{s3_id}", "t1-token"
            )[0] == 200

            assert _request(
                base_url,
                "/api/v1/operator/classes/python-2",
                "operator-token",
                method="POST",
                value={"teachers": ["unknown"], "students": []},
            )[0] == 400
            assert _request(
                base_url,
                "/api/v1/operator/classes/Bad",
                "operator-token",
                method="POST",
                value={"teachers": ["t1"], "students": []},
            )[0] == 400
            assert _request(
                base_url,
                "/api/v1/operator/classes/python-2",
                "wrong",
                method="POST",
                value={"teachers": ["t1"], "students": []},
            )[0] == 401
            roster_log = quote("class/python-1/roster", safe="")
            assert _request(
                base_url,
                f"/api/v1/replica/logs/{roster_log}/records",
                "t1-token",
            )[0] == 403
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("class roster projection and teacher authorization passed")


if __name__ == "__main__":
    main()
