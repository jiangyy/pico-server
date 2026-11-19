"""An event stream that falls behind is closed, so the client reconnects and resynchronizes."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path

from picopg.server.app import build_server
from picopg.server.auth.model import PrincipalAccount
from picopg.server.playgrounds.releases import PLAYGROUND_RELEASES_LOG_ID
from picopg.server.replication import Subscriber


class EventStreamTest(unittest.TestCase):
    def test_a_full_queue_marks_the_subscriber_instead_of_dropping_silently(self) -> None:
        subscriber = Subscriber(lambda _log_id: True, size=1)
        subscriber.offer("first")
        assert not subscriber.overflowed.is_set()
        subscriber.offer("second")
        assert subscriber.overflowed.is_set()
        assert subscriber.events.get_nowait() == "first"

    def test_an_overflowed_stream_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server = build_server(
                Path(temporary), port=0, token="operator-secret", secure_cookies=False,
                principal_accounts=[PrincipalAccount.create("user", "code", ["playgrounds", "drafts"])],
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                login = HTTPConnection("127.0.0.1", port, timeout=2)
                login.request("POST", "/api/v1/session", body=json.dumps({"accessCode": "code"}), headers={"Content-Type": "application/json"})
                response = login.getresponse()
                response.read()
                cookie = "; ".join(value.split(";", 1)[0] for name, value in response.getheaders() if name.lower() == "set-cookie")
                login.close()

                stream = HTTPConnection("127.0.0.1", port, timeout=5)
                stream.request("GET", "/api/v1/replica/events", headers={"Cookie": cookie})
                events = stream.getresponse()
                assert events.status == 200
                assert events.readline() == b": connected\n"
                for _ in range(50):
                    if server._subscribers:
                        break
                    time.sleep(0.01)
                [subscriber] = server._subscribers
                subscriber.overflowed.set()
                server.broadcast(PLAYGROUND_RELEASES_LOG_ID, None)
                # 流要结束（read 返回而不是一直等下去）；落后时写没写出最后那条通知取决于时机，不作要求。
                events.read()
                assert not server._subscribers, "the closed stream unsubscribes"
                stream.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
