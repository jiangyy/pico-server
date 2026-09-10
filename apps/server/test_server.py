"""Tests for the Pico main service (``apps/server/server.py``).

Run with::

    python3 apps/server/test_server.py

Standard library only (``unittest`` + ``http.client``).
"""

import os
import sys
import threading
import unittest
from http.client import HTTPConnection

# Allow ``import server`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import ALLOWED_METHODS, make_server  # noqa: E402


class ServerTest(unittest.TestCase):
    """Black-box tests against a real server on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls.server = make_server(host="127.0.0.1", port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method="GET", path="/", body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body)
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
        headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return status, headers, data

    # -- routes ---------------------------------------------------------

    def test_homepage(self):
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"Pico service is running\n")

    def test_healthz(self):
        status, _, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok\n")

    def test_unknown_path_is_404(self):
        status, _, body = self.request("GET", "/does-not-exist")
        self.assertEqual(status, 404)
        self.assertEqual(body, b"not found\n")

    # -- method boundary ------------------------------------------------

    def test_head_mirrors_get_headers_without_body(self):
        status, headers, body = self.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(
            int(headers["content-length"]), len(b"Pico service is running\n")
        )

    def test_unsupported_methods_are_405(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                status, headers, _ = self.request(method, "/")
                self.assertEqual(status, 405)
                self.assertEqual(headers["allow"], ", ".join(ALLOWED_METHODS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
