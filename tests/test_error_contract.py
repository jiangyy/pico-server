"""Bad input is the client's error on every route: malformed bodies are 400, any credential that does not match is 401."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from picopg.server.ai.access import AiAccess
from picopg.server.app import build_server
from picopg.server.auth.model import PrincipalAccount


class FakeHint:
    def hint(self, *args):
        return {"text": "hint"}


class ErrorContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.server = build_server(
            Path(self.temporary.name), port=0, token="operator-secret", secure_cookies=False,
            principal_accounts=[PrincipalAccount.create("student", "code", ["playgrounds"])],
            ai_access=AiAccess(everyone=True), course_hint=FakeHint(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def call(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> int:
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            # http.client 只收 latin-1 的头：非 ASCII 的值先按 UTF-8 编成字节，像浏览器那样原样发出。
            encoded = {name: value.encode("utf-8").decode("latin-1") for name, value in (headers or {}).items()}
            connection.request(method, path, body=body, headers=encoded)
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def session(self) -> str:
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("POST", "/api/v1/session", body=json.dumps({"accessCode": "code"}), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        connection.close()
        return "; ".join(value.split(";", 1)[0] for name, value in response.getheaders() if name.lower() == "set-cookie")

    def test_malformed_bodies_are_bad_requests_on_every_route(self) -> None:
        cookie = self.session()
        for path in ("/api/v1/ai/hint", "/api/v1/client-log", "/api/v1/session"):
            for body, content_type in ((b"{not json", "application/json"), (b"{}", "text/plain")):
                status = self.call("POST", path, body, {"Content-Type": content_type, "Cookie": cookie})
                assert status == 400, (path, content_type, status)

    def test_a_credential_with_non_ascii_characters_is_simply_wrong(self) -> None:
        json_header = {"Content-Type": "application/json"}
        assert self.call("POST", "/api/v1/session", json.dumps({"accessCode": "访问码"}).encode(), json_header) == 401
        assert self.call("POST", "/api/v1/operator/session", json.dumps({"token": "口令"}).encode(), json_header) == 401
        assert self.call("GET", "/api/v1/session", headers={"Authorization": "Bearer 令牌"}) == 401
        assert self.call("GET", "/api/v1/operator/submissions", headers={"Authorization": "Bearer 令牌"}) == 401


if __name__ == "__main__":
    unittest.main()
