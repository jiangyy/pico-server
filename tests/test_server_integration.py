from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from apps.server.main import build_server
from picopg.api import Position, ReplicatedRecord


TOKEN = "test-replica-token"


class ReplicaServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._start()

    def tearDown(self) -> None:
        self._stop()
        self.temp.cleanup()

    def _start(self) -> None:
        self.server = build_server(
            self.root, host="127.0.0.1", port=0, token=TOKEN
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        host, port = self.server.server_address
        self.address = f"{host}:{port}"

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(
        self,
        method: str,
        path: str,
        value: object | None = None,
        *,
        authorized: bool = True,
    ) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(self.address, timeout=5)
        headers: dict[str, str] = {}
        body = None
        if authorized:
            headers["Authorization"] = f"Bearer {TOKEN}"
        if value is not None:
            body = json.dumps(value, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def append(
        self,
        log_id: str,
        expected_head: dict[str, str] | None,
        records: list[dict[str, object]],
    ) -> tuple[int, dict[str, object]]:
        status, body = self.request(
            "POST",
            f"/api/v1/replica/logs/{log_id}/records",
            {"expectedHead": expected_head, "records": records},
        )
        return status, json.loads(body)

    def test_auth_push_pull_and_restart(self) -> None:
        status, _ = self.request(
            "GET",
            "/api/v1/replica/logs/work/head",
            authorized=False,
        )
        self.assertEqual(status, 401)

        c17 = {
            "id": "C17",
            "logId": "work",
            "parent": None,
            "requires": [],
            "data": {"source": "print(17)"},
        }
        status, result = self.append("work", None, [c17])
        self.assertEqual(status, 200)
        self.assertEqual(result["kind"], "accepted")
        self.assertEqual(
            result["head"], {"logId": "work", "recordId": "C17"}
        )

        status, _ = self.request(
            "HEAD", "/api/v1/replica/logs/work/records/C17"
        )
        self.assertEqual(status, 204)
        status, body = self.request(
            "GET", "/api/v1/replica/logs/work/records?limit=1"
        )
        self.assertEqual(status, 200)
        page = json.loads(body)
        self.assertEqual(page["records"], [c17])
        self.assertFalse(page["hasMore"])

        status, result = self.append("work", None, [c17])
        self.assertEqual((status, result["kind"]), (200, "current"))
        fork = dict(c17, id="fork")
        status, result = self.append("work", None, [fork])
        self.assertEqual((status, result["kind"]), (409, "changed"))

        self._stop()
        self._start()
        status, body = self.request(
            "GET", "/api/v1/replica/logs/work/head"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["head"],
            {"logId": "work", "recordId": "C17"},
        )

    def test_server_owned_result_is_available_to_pull(self) -> None:
        c17 = {
            "id": "C17",
            "logId": "work",
            "parent": None,
            "requires": [],
            "data": {"source": "print(17)"},
        }
        self.append("work", None, [c17])
        q4 = {
            "id": "Q4",
            "logId": "requests",
            "parent": None,
            "requires": [{"logId": "work", "recordId": "C17"}],
            "data": {"type": "analysis.requested"},
        }
        self.append("requests", None, [q4])

        self.server.store.append(
            "results",
            None,
            (
                ReplicatedRecord(
                    id="R7",
                    log_id="results",
                    parent=None,
                    requires=(
                        Position("requests", "Q4"),
                        Position("work", "C17"),
                    ),
                    data={
                        "type": "analysis.completed",
                        "code": {
                            "logId": "work",
                            "recordId": "C17",
                        },
                    },
                ),
            ),
        )
        status, body = self.request(
            "GET", "/api/v1/replica/logs/results/records?limit=10"
        )
        self.assertEqual(status, 200)
        result = json.loads(body)["records"][0]
        self.assertEqual(result["id"], "R7")
        self.assertEqual(
            result["requires"],
            [
                {"logId": "requests", "recordId": "Q4"},
                {"logId": "work", "recordId": "C17"},
            ],
        )

    def test_missing_dependency_is_not_partially_written(self) -> None:
        result = {
            "id": "R7",
            "logId": "results",
            "parent": None,
            "requires": [{"logId": "requests", "recordId": "missing"}],
            "data": {},
        }
        status, _ = self.append("results", None, [result])
        self.assertEqual(status, 400)
        status, body = self.request(
            "GET", "/api/v1/replica/logs/results/head"
        )
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["head"])


if __name__ == "__main__":
    unittest.main()
