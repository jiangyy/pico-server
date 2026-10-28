from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.client import HTTPConnection
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for entry in (ROOT, SRC):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from picopg.server.auth.model import PrincipalAccount  # noqa: E402
from picopg.server.app import build_server  # noqa: E402


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


def main() -> None:
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

    print("browser and native Session bootstrap, logout, Replica restore and operator Session passed")


if __name__ == "__main__":
    main()
