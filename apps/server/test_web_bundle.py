"""HTTP tests for the optional Web bundle served from the data directory.

The Server is the only process nginx talks to, so the Web product is served
from the same origin as the API: one startup document plus ``main.js``,
``picopg.css`` and ``pico.svg``.  These tests pin the serving contract only —
the file *contents* belong to the Web build and are not asserted here.

What is pinned:

1. Every path in ``WEB_BUNDLE`` is served with its declared content type.
2. An absent bundle keeps the historical ``/`` placeholder (so an
   un-provisioned host still answers) and 404s every other bundle path.
3. A caller-supplied name can never escape ``web_path``.
4. Revalidation works: a matching ``If-None-Match`` returns 304 with no body.
"""

import hashlib
import http.client
import io
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from server import HOST, WEB_BUNDLE, make_server


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


class WebBundleTest(unittest.TestCase):
    """Each test owns its data directory, bundle contents and Server."""

    def _start(self, *, bundle=None):
        self.temporary_directory = TemporaryDirectory()
        base = Path(self.temporary_directory.name)
        self.database_path = base / "app.db"
        self.repository_path = base / "picopg.git"
        self.web_path = base / "web"
        if bundle is not None:
            self.web_path.mkdir(parents=True)
            for filename, data in bundle.items():
                self.web_path.joinpath(filename).write_bytes(data)
        self.port = _free_port()
        self.server = make_server(
            host=HOST,
            port=self.port,
            database_path=self.database_path,
            repository_path=self.repository_path,
            web_path=self.web_path,
            audit_output=io.StringIO(),
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary_directory.cleanup()

    def _get(self, path, headers=None):
        connection = http.client.HTTPConnection(HOST, self.port, timeout=5)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            body = response.read()
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    def _head(self, path):
        connection = http.client.HTTPConnection(HOST, self.port, timeout=5)
        try:
            connection.request("HEAD", path)
            response = connection.getresponse()
            body = response.read()
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    # --- tests ----------------------------------------------------------

    def test_every_bundle_path_is_served_with_its_content_type(self):
        bundle = {
            "index.html": b"<!doctype html><title>Pico</title>",
            "main.js": b"console.log('pico');",
            "picopg.css": b"body{margin:0}",
            "pico.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        }
        self._start(bundle=bundle)

        for path, (filename, content_type) in WEB_BUNDLE.items():
            status, headers, body = self._get(path)
            self.assertEqual(status, 200, f"{path} -> {status}")
            self.assertEqual(headers.get("Content-Type"), content_type)
            self.assertEqual(body, bundle[filename], f"{path} bytes")
            # The content hash is the validator; the header must carry it.
            expected = hashlib.sha256(bundle[filename]).hexdigest()
            self.assertEqual(headers.get("ETag"), f'"{expected}"')

    def test_absent_bundle_keeps_placeholder_and_404s_the_rest(self):
        self._start(bundle=None)

        status, _, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"Pico service is running\n")

        for path in ("/main.js", "/picopg.css", "/pico.svg", "/index.html"):
            status, _, _ = self._get(path)
            self.assertEqual(status, 404, f"{path} should be 404 without a bundle")

    def test_unknown_paths_are_not_served_from_the_bundle_directory(self):
        self._start(
            bundle={
                "index.html": b"<html></html>",
                "main.js": b"",
                "picopg.css": b"",
                "pico.svg": b"",
            }
        )
        # A file that exists on disk but is not in the closed table.
        self.web_path.joinpath("secret.txt").write_bytes(b"do-not-serve")

        for path in ("/secret.txt", "/../secret.txt", "/web/secret.txt"):
            status, _, _ = self._get(path)
            self.assertEqual(status, 404, f"{path} must not be served")

    def test_if_none_match_revalidates_without_a_body(self):
        content = b"body{margin:0}"
        self._start(bundle={"index.html": b"", "main.js": b"", "picopg.css": content, "pico.svg": b""})
        etag = hashlib.sha256(content).hexdigest()

        status, headers, _ = self._get(
            "/picopg.css", headers={"If-None-Match": f'"{etag}"'}
        )
        self.assertEqual(status, 304)
        self.assertNotIn("Content-Length", headers)

    def test_head_returns_headers_without_a_body(self):
        self._start(bundle={"index.html": b"<html></html>", "main.js": b"", "picopg.css": b"", "pico.svg": b""})

        status, headers, body = self._head("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(
            headers.get("Content-Length"), str(len(b"<html></html>"))
        )
        self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
