from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from picopg.replica.file_store import FileReplicaLogStore
from picopg.server.app import PicoPGHttpServer
from picopg.server.auth.model import PrincipalSession
from picopg.server.client_log import ClientLogLimiter


def post(connection, token, body):
    connection.request(
        "POST",
        "/api/v1/client-log",
        json.dumps(body).encode(),
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    response = connection.getresponse()
    return response.status, json.loads(response.read())


ENTRY = {"level": "warn", "page": "/", "text": "课程 AI 失败，改用离线回答 Error: HTTP 503"}


class ClientLogRouteTest(unittest.TestCase):
    def test_signed_in_clients_log_valid_entries_within_a_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = PrincipalSession.create("student", "device", "student-token", {}, ["playgrounds"])
            server = PicoPGHttpServer(
                ("127.0.0.1", 0),
                FileReplicaLogStore(root / "replica"),
                "operator-token",
                root / "packages",
                principal_sessions=[session],
            )
            server.client_log_limiter = ClientLogLimiter(per_minute=2)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(*server.server_address)
            try:
                with self.assertLogs("picopg.client", "WARNING") as logged:
                    status, _ = post(connection, "student-token", ENTRY)
                assert status == 200
                assert "student/device" in logged.output[0] and "改用离线回答" in logged.output[0]
                status, error = post(connection, "wrong-token", ENTRY)
                assert status == 401 and error["error"]["code"] == "auth.required"
                status, error = post(connection, "student-token", {**ENTRY, "level": "info"})
                assert status == 400 and error["error"]["code"] == "client_log.invalid"
                with self.assertLogs("picopg.client", "WARNING"):
                    post(connection, "student-token", ENTRY)
                status, error = post(connection, "student-token", ENTRY)
                assert status == 429 and error["error"]["code"] == "client_log.limited"
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join()


class ClientLogLimiterTest(unittest.TestCase):
    def test_budget_resets_every_minute(self) -> None:
        now = [0.0]
        limiter = ClientLogLimiter(per_minute=1, clock=lambda: now[0])
        assert limiter.allow("a") and not limiter.allow("a") and limiter.allow("b")
        now[0] = 61
        assert limiter.allow("a")
        assert set(limiter.windows) == {"a"}, "devices that stopped logging do not stay in the table"

    def test_client_entries_go_to_their_own_file_once(self) -> None:
        import logging
        import tempfile
        from pathlib import Path
        from picopg.server.client_log import client_logger, write_client_log_to
        with tempfile.TemporaryDirectory() as temporary:
            write_client_log_to(Path(temporary) / "a.log")
            write_client_log_to(Path(temporary) / "client.log")
            assert len(client_logger.handlers) == 1 and client_logger.propagate is False
            client_logger.warning("one entry")
            for handler in client_logger.handlers:
                handler.flush()
            assert (Path(temporary) / "client.log").read_text(encoding="utf-8").count("one entry") == 1
            for handler in list(client_logger.handlers):
                client_logger.removeHandler(handler)
                handler.close()
            assert isinstance(logging.getLogger("picopg.client"), logging.Logger)
