


"""Contract test for server-owned Draft comments.

Runs an in-process PicoPGHttpServer and checks that an operator can append a comment
that Clients can read, and that Clients cannot write it.
Stdlib-only; exits 0 on success and non-zero on failure.
"""

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
from picopg.server.review.comments import comment_log_id
from picopg.replica.file_store import FileReplicaLogStore


def _start(tmp: Path) -> tuple[PicoPGHttpServer, str]:
    server = PicoPGHttpServer(
        ("127.0.0.1", 0),
        FileReplicaLogStore(tmp / "replica"),
        "operator-token",
        tmp / "packages",
        bindings=[(comment_log_id("user", "lesson"), "read")],
    )
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server._test_thread = thread  # type: ignore[attr-defined]
    return server, f"http://{host}:{port}"


def _stop(server: PicoPGHttpServer) -> None:
    server.shutdown()
    server.server_close()
    server._test_thread.join(timeout=2)  # type: ignore[attr-defined]


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _call(
    url: str,
    *,
    method: str = "GET",
    body: object | None = None,
    token: str = "operator-token",
) -> tuple[int, object]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read() or b"null")


def _comments(base_url: str) -> None:
    log_id = comment_log_id("user", "lesson")
    route = quote(log_id, safe="")

    status, created = _call(
        f"{base_url}/api/v1/principals/user/drafts/lesson/comments",
        method="POST",
        body={"author": "teacher", "text": "try a loop here"},
    )
    _expect(status == 201, f"comment append returned {status}")
    first = created["position"]  # type: ignore[index]
    _expect(first["logId"] == log_id, f"comment landed in {first['logId']}")

    status, _ = _call(
        f"{base_url}/api/v1/principals/user/drafts/lesson/comments",
        method="POST",
        body={"author": "teacher", "text": "second"},
    )
    _expect(status == 201, f"second comment append returned {status}")

    status, page = _call(f"{base_url}/api/v1/replica/logs/{route}/records?limit=10")
    _expect(status == 200, f"comment read returned {status}")
    records = page["records"]  # type: ignore[index]
    _expect(len(records) == 2, f"expected 2 comments, got {len(records)}")
    _expect(records[1]["parent"] == first, "comments must form a single chain")
    _expect(records[0]["data"]["text"] == "try a loop here", "comment text changed on the way out")
    _expect(records[0]["data"]["draftId"] == "lesson", "comment lost its Draft")
    _expect(records[0]["requires"] == [], "comments must not depend on Replica Logs")

    status, invalid = _call(
        f"{base_url}/api/v1/principals/user/drafts/lesson/comments",
        method="POST",
        body={"author": "teacher", "text": "   "},
    )
    _expect(status == 400, f"empty text returned {status}")
    _expect(invalid["error"]["code"] == "comment.invalid", "comment error code changed")

    status, _ = _call(
        f"{base_url}/api/v1/principals/user/drafts/lesson/comments",
        method="POST",
        body={"author": "teacher", "text": "nope"},
        token="client-token",
    )
    _expect(status == 401, f"a client token could append a comment ({status})")

    status, _ = _call(
        f"{base_url}/api/v1/replica/logs/{route}/records?limit=10",
        method="POST",
        body={"expectedHead": None, "records": []},
    )
    _expect(status == 403, f"the comment Log accepted an append over the replica route ({status})")


class CommentsTest(unittest.TestCase):
    def test_comments(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            server, base_url = _start(Path(raw_dir))
            try:
                _comments(base_url)
            finally:
                _stop(server)


if __name__ == "__main__":
    unittest.main()
