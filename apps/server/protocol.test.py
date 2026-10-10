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


def _read_sse(response, deadline: float) -> str:
    chunks: list[bytes] = []
    while _monotonic() < deadline:
        buffered = response.fp.peek(4096)
        if buffered:
            chunks.append(buffered)
            response.fp.read(len(buffered))
            if b"\n\n" in b"".join(chunks):
                break
            continue
        response.fp.raw._sock.settimeout(max(0.05, deadline - _monotonic()))
        try:
            chunk = response.fp.raw._sock.recv(4096)
        except (TimeoutError, OSError):
            break
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n\n" in b"".join(chunks):
            break
    return b"".join(chunks).decode("utf-8", errors="replace")


def _monotonic() -> float:
    import time
    return time.monotonic()


def _append_and_broadcast(tmp: Path) -> None:
    from picopg.api import record_id_of, Position, ReplicatedRecord
    server, base_url = _start_server(tmp, bindings=[("owned", "push")])
    host, port = server.server_address
    try:
        conn = HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/api/v1/replica/events",
                     headers={"Authorization": "Bearer secret"})
        response = conn.getresponse()
        _expect(response.status == 200, f"events status was {response.status}")
        _read_sse(response, _monotonic() + 1)

        record_id = record_id_of("owned", None, (), (), {"value": 1})
        record = ReplicatedRecord(
            id=record_id, log_id="owned", parent=None,
            requires=(), objects=(), data={"value": 1},
        )
        result = server.append_and_broadcast("owned", None, (record,))
        _expect(result.kind == "accepted", f"append_and_broadcast returned {result.kind}")
        _expect(server.store.has(Position("owned", record_id)), "store did not retain record")

        body = _read_sse(response, _monotonic() + 2)
        _expect('"logId":"owned"' in body, f"event logId missing in {body!r}")
        _expect(f'"recordId":"{record_id}"' in body, f"event recordId missing in {body!r}")
        response.close()
        conn.close()
    finally:
        _stop(server)


def _push_channel(tmp: Path) -> None:
    server, base_url = _start_server(
        tmp, bindings=[("owned", "push"), ("mirror", "pull")]
    )
    host, port = server.server_address
    auth = {"Authorization": "Bearer secret"}
    stream_conn = HTTPConnection(host, port, timeout=5)
    stream_conn.request("GET", "/api/v1/replica/events", headers=auth)
    stream_response = stream_conn.getresponse()
    try:
        _expect(stream_response.status == 200,
                f"events status was {stream_response.status}")
        _expect(
            stream_response.getheader("Content-Type", "").startswith("text/event-stream"),
            f"events content-type was {stream_response.getheader('Content-Type')!r}",
        )

        preamble = _read_sse(stream_response, _monotonic() + 1)
        _expect(preamble.startswith(": "), f"events preamble was {preamble!r}")

        from picopg.api import record_id_of, Position, ReplicatedRecord
        record_id = record_id_of("owned", None, (), (), {"value": 1})
        record = ReplicatedRecord(
            id=record_id,
            log_id="owned",
            parent=None,
            requires=(),
            objects=(),
            data={"value": 1},
        )
        head_position = Position("owned", record.id)

        result = server.store.append("owned", None, (record,))
        _expect(result.kind == "accepted", f"direct append returned {result.kind}")
        server.broadcast("owned", result.head)

        event = _read_sse(stream_response, _monotonic() + 2)
        _expect('"logId":"owned"' in event, f"event logId missing in {event!r}")
        _expect(f'"recordId":"{head_position.record_id}"' in event,
                f"event head recordId missing in {event!r}")

        no_auth_conn = HTTPConnection(host, port, timeout=2)
        no_auth_conn.request("GET", "/api/v1/replica/events")
        no_auth_response = no_auth_conn.getresponse()
        _expect(no_auth_response.status == 401,
                f"events without auth returned {no_auth_response.status}")
        no_auth_response.read()
        no_auth_conn.close()

        stream_response.close()
        stream_conn.close()
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

    with tempfile.TemporaryDirectory() as raw_dir:
        _push_channel(Path(raw_dir))

    with tempfile.TemporaryDirectory() as raw_dir:
        _append_and_broadcast(Path(raw_dir))

    print("server handshake, public endpoint, binding enforcement, push events and append_and_broadcast passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())