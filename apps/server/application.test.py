"""End-to-end contract test for Playground publication."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for _entry in (_ROOT, _SRC):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from apps.server.main import build_server  # noqa: E402
from apps.server.application import PicoPGHttpServer  # noqa: E402
from apps.server.playground.catalog import PLAYGROUND_RELEASES_LOG_ID  # noqa: E402
from apps.server.playground.package import PackageInfo  # noqa: E402
from picopg.impl.file_replica_log import FileReplicaLogStore  # noqa: E402


def _package(title: str, course: str = "course") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "picopg.json",
            json.dumps(
                {
                    "schemaVersion": 1,
                    "runtime": "python",
                    "module": "course/demo.js",
                    "title": title,
                }
            ),
        )
        archive.writestr("course/index.html", course)
        archive.writestr("course/demo.js", "export const starter = ''; export const Root = () => null;")
        archive.writestr("starter/main.py", "print('hello')\n")
    return output.getvalue()


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        base_url + path,
        method=method,
        data=body,
        headers={"Authorization": "Bearer secret", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _replaceable_package_format(root: Path) -> None:
    class FuturePackageAdapter:
        media_type = "application/x-pico"
        max_bytes = 64

        def inspect(self, payload: bytes) -> PackageInfo:
            if payload != b"future-package":
                raise AssertionError("adapter received different bytes")
            return PackageInfo(self.media_type, "Future")

    server = PicoPGHttpServer(
        ("127.0.0.1", 0),
        FileReplicaLogStore(root / "replica"),
        "secret",
        root / "packages",
        package_adapter=FuturePackageAdapter(),
    )
    try:
        result = server.playgrounds.publish(
            "future", None, "application/x-pico", b"future-package"
        )
        _expect(result.status == "accepted", "replacement adapter did not publish")
        release = server.playgrounds.catalog()[0]
        _expect(
            release.media_type == "application/x-pico",
            "replacement media type did not reach the release",
        )
        media_type, payload = server.playgrounds.package(
            "future", result.version_id
        )
        _expect(
            media_type == "application/x-pico" and payload == b"future-package",
            "replacement package could not be read",
        )
    finally:
        server.server_close()


def main() -> int:
    with tempfile.TemporaryDirectory() as raw_dir:
        _replaceable_package_format(Path(raw_dir))

    with tempfile.TemporaryDirectory() as raw_dir:
        root = Path(raw_dir)
        server = build_server(root, port=0, token="secret")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        try:
            _expect(
                server.bindings.get(PLAYGROUND_RELEASES_LOG_ID) == "read",
                "server-owned release Log was not exposed read-only",
            )
            status, _, body = _request(base_url, "/api/v1/catalog")
            _expect(status == 200, f"empty catalog returned {status}")
            _expect(json.loads(body) == {"playgrounds": []}, "catalog was not empty")

            first = _package("First")
            status, _, body = _request(
                base_url,
                "/api/v1/playgrounds/demo/versions",
                method="POST",
                body=first,
                headers={"Content-Type": "application/zip"},
            )
            payload = json.loads(body)
            _expect(status == 201, f"first publish returned {status}: {body!r}")
            _expect(payload["status"] == "accepted", f"publish was {payload!r}")
            first_version = payload["version_id"]

            status, _, body = _request(base_url, "/api/v1/catalog")
            catalog = json.loads(body)["playgrounds"]
            _expect(status == 200 and len(catalog) == 1, "published catalog is wrong")
            _expect(catalog[0]["playground_id"] == "demo", "catalog id is wrong")
            _expect(catalog[0]["current_version_id"] == first_version, "catalog version is wrong")
            _expect(catalog[0]["title"] == "First", "catalog title is wrong")

            status, headers, body = _request(
                base_url,
                f"/api/v1/playgrounds/demo/versions/{first_version}/package",
            )
            _expect(status == 200 and body == first, "download did not return stored bytes")
            _expect(headers["Content-Type"] == "application/zip", "download media type is wrong")

            status, _, body = _request(
                base_url,
                "/api/v1/playgrounds/demo/versions",
                method="POST",
                body=first,
                headers={"Content-Type": "application/zip"},
            )
            _expect(status == 200, f"identical publish returned {status}")
            _expect(json.loads(body)["status"] == "no_op", "identical publish was not a no-op")

            second = _package("Second", "changed")
            status, _, body = _request(
                base_url,
                "/api/v1/playgrounds/demo/versions",
                method="POST",
                body=second,
                headers={"Content-Type": "application/zip"},
            )
            _expect(status == 409, f"stale publish returned {status}")
            _expect(json.loads(body)["version_id"] == first_version, "conflict omitted current version")

            status, _, body = _request(
                base_url,
                "/api/v1/playgrounds/demo/versions",
                method="POST",
                body=second,
                headers={
                    "Content-Type": "application/zip",
                    "X-Base-Version-Id": first_version,
                },
            )
            _expect(status == 201, f"based publish returned {status}: {body!r}")
            _expect(json.loads(body)["status"] == "accepted", "based publish failed")

            status, _, body = _request(
                base_url,
                "/api/v1/playgrounds/bad/versions",
                method="POST",
                body=b"not a zip",
                headers={"Content-Type": "application/zip"},
            )
            _expect(status == 400, f"invalid ZIP returned {status}")
            _expect(json.loads(body)["error"]["code"] == "package.invalid", "invalid ZIP error is wrong")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        restarted = build_server(root, port=0, token="secret")
        try:
            catalog = restarted.playgrounds.catalog()
            _expect(len(catalog) == 1 and catalog[0].title == "Second", "release Log was not durable")
            media_type, payload = restarted.playgrounds.package(
                "demo", catalog[0].version_id
            )
            _expect(media_type == "application/zip" and payload == second, "package was not durable")
        finally:
            restarted.server_close()

    print("playground format seam, validation, publish, conflict, download and restart passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
