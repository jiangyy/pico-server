"""Pico main service — first version, Python standard library only.

Listens on ``127.0.0.1:8001`` and exposes only two routes:

* ``/``        — homepage
* ``/healthz`` — health check

Method boundary: only ``GET`` and ``HEAD`` are supported; every other
method returns ``405 Method Not Allowed``. Unknown paths return ``404``.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HOST = "127.0.0.1"
PORT = 8001

ALLOWED_METHODS = ("GET", "HEAD")


class Handler(BaseHTTPRequestHandler):
    """Minimal route handler with an explicit method boundary."""

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route(head_only=True)

    def _route(self, head_only=False):
        path = urlsplit(self.path).path

        if path == "/":
            status, body = 200, "Pico service is running\n"
        elif path == "/healthz":
            status, body = 200, "ok\n"
        else:
            status, body = 404, "not found\n"

        self._respond(status, body, head_only=head_only)

    def _respond(self, status, text, head_only=False, extra_headers=()):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _method_not_allowed(self):
        self._respond(
            405,
            "method not allowed\n",
            extra_headers=(("Allow", ", ".join(ALLOWED_METHODS)),),
        )

    # Method boundary: everything except GET/HEAD is rejected with 405.
    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_OPTIONS = _method_not_allowed


def make_server(host=HOST, port=PORT):
    """Return a ``ThreadingHTTPServer`` bound to ``host:port``.

    ``port=0`` asks the OS for an ephemeral port, which the tests use to
    avoid clashing with the running service.
    """
    return ThreadingHTTPServer((host, port), Handler)


def main():
    server = make_server()
    host, port = server.server_address[:2]
    print(f"Pico service: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
