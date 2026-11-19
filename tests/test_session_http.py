from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from picopg.server.app import build_server
from picopg.server.auth.model import PrincipalAccount


def _request(
    server,
    method: str,
    path: str,
    *,
    value: object | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, list[tuple[str, str]], object]:
    body = None if value is None else json.dumps(value).encode()
    request_headers = dict(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        return response.status, response.getheaders(), None if not payload else json.loads(payload)
    finally:
        connection.close()


def _cookies(headers: list[tuple[str, str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers:
        if name.lower() != "set-cookie":
            continue
        key, token = value.split(";", 1)[0].split("=", 1)
        result[key] = token
    return result


def _start(root: Path):
    server = build_server(
        root,
        port=0,
        token="operator-secret",
        principal_accounts=[PrincipalAccount.create(
            "user", "student-code", ["playgrounds", "drafts", "submissions", "analysis"],
        )],
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


class SessionHttpTest(unittest.TestCase):
    def test_a_device_that_lost_its_storage_becomes_a_new_device(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, thread = _start(Path(temporary))
            try:
                status, headers, first = _request(server, "POST", "/api/v1/session", value={"accessCode": "student-code"})
                assert status == 201
                cookies = _cookies(headers)
                cookie = "; ".join(f"{name}={value}" for name, value in cookies.items())
                status, headers, renewed = _request(server, "POST", "/api/v1/session/device", headers={"Cookie": cookie})
                assert status == 201
                assert renewed["principalId"] == first["principalId"] and renewed["replicaId"] != first["replicaId"]
                fresh = _cookies(headers)
                assert fresh["picopg_device"] != cookies["picopg_device"]
                # 旧会话作废，新会话认新设备。
                assert _request(server, "GET", "/api/v1/session", headers={"Cookie": f"picopg_session={cookies['picopg_session']}"})[0] == 401
                status, _, current = _request(server, "GET", "/api/v1/session", headers={"Cookie": f"picopg_session={fresh['picopg_session']}"})
                assert (status, current["replicaId"]) == (200, renewed["replicaId"])
                assert _request(server, "POST", "/api/v1/session/device")[0] == 401
                assert _request(server, "GET", "/api/v1/session/device", headers={"Cookie": f"picopg_session={fresh['picopg_session']}"})[0] == 405
            finally:
                _stop(server, thread)

    def test_repeated_wrong_access_codes_are_slowed_down(self) -> None:
        from picopg.server.auth.limiter import LoginLimiter
        with tempfile.TemporaryDirectory() as temporary:
            server, thread = _start(Path(temporary))
            now = [0.0]
            server.login_limiter = LoginLimiter(failures=3, window=60, clock=lambda: now[0])
            try:
                for _ in range(3):
                    assert _request(server, "POST", "/api/v1/session", value={"accessCode": "guess"})[0] == 401
                # 拦下之后连对的访问码也不收，否则猜的人一直猜到对为止。
                assert _request(server, "POST", "/api/v1/session", value={"accessCode": "student-code"})[0] == 429
                assert _request(server, "POST", "/api/v1/operator/session", value={"token": "guess"})[0] == 429
                # 隔壁机器（nginx 转来的另一个地址）不受影响。
                assert _request(server, "POST", "/api/v1/session", value={"accessCode": "student-code"}, headers={"X-Real-IP": "10.0.0.7"})[0] == 201
                now[0] = 61
                assert _request(server, "POST", "/api/v1/session", value={"accessCode": "student-code"})[0] == 201
            finally:
                _stop(server, thread)

    def test_session_http(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server, thread = _start(root)
            try:
                status, _, _ = _request(server, "GET", "/api/v1/session")
                assert status == 401
                status, _, _ = _request(
                    server, "POST", "/api/v1/session", value={"accessCode": "wrong"},
                )
                assert status == 401

                status, headers, identity = _request(
                    server, "POST", "/api/v1/session", value={"accessCode": "student-code"},
                )
                assert status == 201
                cookies = _cookies(headers)
                assert set(cookies) == {"picopg_session", "picopg_device"}
                joined = "\n".join(value for name, value in headers if name.lower() == "set-cookie")
                assert "HttpOnly" in joined and "SameSite=Strict" in joined and "Secure" in joined
                cookie = f"picopg_session={cookies['picopg_session']}; picopg_device={cookies['picopg_device']}"

                status, _, current = _request(
                    server, "GET", "/api/v1/replica/identity", headers={"Cookie": cookie},
                )
                assert status == 200 and current == identity
                principal = identity["principalId"]
                status, _, _ = _request(
                    server,
                    "GET",
                    f"/api/v1/replica/logs/principal%2F{principal}%2Fdraft%2Flesson%2Fdecision/head",
                    headers={"Cookie": cookie},
                )
                assert status == 200
                status, _, _ = _request(
                    server,
                    "GET",
                    "/api/v1/replica/logs/principal%2Fother%2Fdraft%2Flesson%2Fdecision/head",
                    headers={"Cookie": cookie},
                )
                assert status == 403

                status, operator_headers, _ = _request(
                    server, "POST", "/api/v1/operator/session", value={"token": "operator-secret"},
                )
                assert status == 201
                operator_cookie = _cookies(operator_headers)["picopg_operator_session"]
                status, _, catalog = _request(
                    server,
                    "GET",
                    "/api/v1/catalog",
                    headers={"Cookie": f"picopg_operator_session={operator_cookie}"},
                )
                assert status == 200 and catalog == {"playgrounds": []}

                status, _, _ = _request(
                    server, "DELETE", "/api/v1/session", headers={"Cookie": cookie},
                )
                assert status == 200
                status, _, _ = _request(
                    server, "GET", "/api/v1/replica/identity", headers={"Cookie": cookie},
                )
                assert status == 401
                status, headers, restored = _request(
                    server,
                    "POST",
                    "/api/v1/session",
                    value={"accessCode": "student-code"},
                    headers={"Cookie": f"picopg_device={cookies['picopg_device']}"},
                )
                assert status == 201 and restored == identity
                restarted_cookie = _cookies(headers)["picopg_session"]

                # Native clients get their tokens in the body and present the session as a Bearer token.
                native = {"X-Pico-Client": "native"}
                status, headers, issued = _request(
                    server, "POST", "/api/v1/session", value={"accessCode": "student-code"}, headers=native,
                )
                assert status == 201 and not _cookies(headers)
                assert set(issued) == {"principalId", "replicaId", "sessionToken", "deviceToken", "expiresAt"}
                bearer = {"Authorization": f"Bearer {issued['sessionToken']}"}
                status, _, current = _request(server, "GET", "/api/v1/replica/identity", headers=bearer)
                assert status == 200 and current["replicaId"] == issued["replicaId"]
                status, _, again = _request(
                    server, "POST", "/api/v1/session",
                    value={"accessCode": "student-code", "deviceToken": issued["deviceToken"]}, headers=native,
                )
                assert status == 201 and again["replicaId"] == issued["replicaId"], "the device token restores the Replica"
                status, _, _ = _request(server, "DELETE", "/api/v1/session", headers=bearer)
                assert status == 200
                status, _, _ = _request(server, "GET", "/api/v1/replica/identity", headers=bearer)
                assert status == 401
                status, _, _ = _request(
                    server, "POST", "/api/v1/session",
                    value={"accessCode": "student-code", "deviceToken": issued["deviceToken"]},
                )
                assert status == 400, "browsers send the device token only as a cookie"
            finally:
                _stop(server, thread)

            server, thread = _start(root)
            try:
                status, _, current = _request(
                    server,
                    "GET",
                    "/api/v1/replica/identity",
                    headers={"Cookie": f"picopg_session={restarted_cookie}"},
                )
                assert status == 200 and current == identity
            finally:
                _stop(server, thread)


if __name__ == "__main__":
    unittest.main()
