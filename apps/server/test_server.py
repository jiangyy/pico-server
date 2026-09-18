"""Tests for the PicoPG HTTP service skeleton.

The feature contract lives in ``test_api.py``.  This file only checks the
things that hold for every request: the health endpoint, the 404 and 405
vocabulary, HEAD mirroring, and the bare repository the server is handed.
"""

import os
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

# Allow ``import server`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import ALLOWED_METHODS, make_server  # noqa: E402

OPERATOR_TOKEN = "test-operator-token-value"


class ServerTest(unittest.TestCase):
    """Black-box tests against a real server on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(cls.temporary_directory.name) / "app.db"
        cls.repository_path = Path(cls.temporary_directory.name) / "picopg.git"
        cls.server = make_server(
            host="127.0.0.1",
            port=0,
            database_path=database_path,
            repository_path=cls.repository_path,
            operator_token=OPERATOR_TOKEN,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temporary_directory.cleanup()

    def request(self, method="GET", path="/", body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body)
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
        headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return status, headers, data

    def test_healthz(self):
        status, _, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok\n")

    def test_unknown_path_is_404(self):
        status, _, body = self.request("GET", "/does-not-exist")
        self.assertEqual(status, 404)
        self.assertEqual(body, b"not found\n")

    def test_head_mirrors_get_headers_without_body(self):
        status, headers, body = self.request("HEAD", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(headers["content-length"]), len(b"ok\n"))

    def test_unsupported_methods_are_405(self):
        for method in ("PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                status, headers, _ = self.request(method, "/healthz")
                self.assertEqual(status, 405)
                self.assertEqual(headers["allow"], ", ".join(ALLOWED_METHODS))

    def test_injected_repository_is_initialized_bare(self):
        self.assertEqual(self.server.repository_path, self.repository_path)
        self.assertTrue(self.repository_path.is_dir())
        self.assertTrue((self.repository_path / "HEAD").is_file())
        self.assertFalse((self.repository_path / ".git").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
