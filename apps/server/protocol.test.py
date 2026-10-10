"""Contract test for the replica HTTP server.

Runs an in-process ReplicaHttpServer and probes its public endpoints.
Stdlib-only; exits 0 on success and non-zero on failure.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for _entry in (_ROOT, _SRC):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from apps.server.protocol import (  # noqa: E402
    HANDSHAKE_CAPABILITIES,
    PROTOCOL_VERSION,
    ReplicaHttpServer,
)
from picopg.impl.file_replica_log import FileReplicaLogStore  # noqa: E402


def _start_server(
    tmp: Path,
    token: str = "secret",
    bindings: list[tuple[str, str]] | None = None,
) -> tuple[ReplicaHttpServer, str]:
    store = FileReplicaLogStore(tmp / "replica")
    server = ReplicaHttpServer(("127.0.0.1", 0), store, token, bindings=bindings)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server._test_thread = thread  # type: ignore[attr-defined]
    return server, f"http://{host}:{port}"


def _get(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, method="GET", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read() or b""


def _stop(server: ReplicaHttpServer) -> None:
    server.shutdown()
    server.server_close()
    server._test_thread.join(timeout=2)  # type: ignore[attr-defined]


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _handshake_and_auth(tmp: Path) -> tuple[ReplicaHttpServer, str]:
    server, base_url = _start_server(
        tmp,
        bindings=[("work", "push"), ("results", "push"), ("requests", "pull")],
    )
    status, headers, body = _get(f"{base_url}/api/v1/replica/handshake")
    _expect(status == 200, f"handshake status was {status}, expected 200")
    _expect(
        headers.get("Content-Type", "").startswith("application/json"),
        f"handshake content-type was {headers.get('Content-Type')!r}",
    )
    payload = json.loads(body.decode("utf-8"))
    _expect(payload["protocol"] == PROTOCOL_VERSION,
            f"handshake protocol was {payload['protocol']}, expected {PROTOCOL_VERSION}")
    _expect(payload["server"] == "picopg-server",
            f"handshake server was {payload['server']!r}")
    _expect(payload["capabilities"] == list(HANDSHAKE_CAPABILITIES),
            f"handshake capabilities were {payload['capabilities']!r}")
    _expect(set(payload.keys()) == {"protocol", "server", "capabilities"},
            f"handshake keys were {sorted(payload.keys())}")

    status, _, _ = _get(f"{base_url}/api/v1/replica/handshake",
                        headers={"Authorization": "Bearer should-be-ignored"})
    _expect(status == 200, "handshake must not require Authorization")

    conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
    try:
        conn.request("POST", "/api/v1/replica/handshake", body=b"")
        response = conn.getresponse()
        _expect(response.status == 405,
                f"handshake POST status was {response.status}, expected 405")
    finally:
        conn.close()
    return server, base_url


def _bindings_enforcement(tmp: Path) -> None:
    server, base_url = _start_server(
        tmp, bindings=[("owned", "push"), ("mirror", "pull")]
    )
    try:
        auth = {"Authorization": "Bearer secret"}

        status, _, _ = _get(
            f"{base_url}/api/v1/replica/logs/owned/head", headers=auth,
        )
        _expect(status == 200, f"bound head returned {status}")

        status, _, body = _get(
            f"{base_url}/api/v1/replica/logs/mirror/head", headers=auth,
        )
        _expect(status == 403, f"mirror log must reject head, got {status}")
        payload = json.loads(body.decode("utf-8"))
        _expect(payload["error"]["code"] == "capability.denied",
                f"expected capability.denied, got {payload['error']['code']}")

        status, _, _ = _get(
            f"{base_url}/api/v1/replica/logs/secret/head", headers=auth,
        )
        _expect(status == 403, f"unbound log returned {status}")

        conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
        try:
            conn.request(
                "POST",
                "/api/v1/replica/logs/owned/records",
                body=b'{"expectedHead":null,"records":[]}',
                headers={**auth, "Content-Type": "application/json"},
            )
            response = conn.getresponse()
            response.read()
            _expect(response.status == 403,
                    f"push to push-bound log returned {response.status}")
        finally:
            conn.close()
    finally:
        _stop(server)


def main() -> int:
    with tempfile.TemporaryDirectory() as raw_dir:
        root = Path(raw_dir)
        server, base_url = _handshake_and_auth(root)
        try:
            status, _, _ = _get(f"{base_url}/api/v1/replica/logs/work/head")
            _expect(status == 401, f"protected route returned {status} without token")

            status, _, _ = _get(
                f"{base_url}/api/v1/replica/logs/work/head",
                headers={"Authorization": "Bearer secret"},
            )
            _expect(status == 200, f"protected route returned {status} with token")
        finally:
            _stop(server)

    with tempfile.TemporaryDirectory() as raw_dir:
        _bindings_enforcement(Path(raw_dir))

    print("server handshake, public endpoint and binding enforcement passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())