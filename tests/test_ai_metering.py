"""The budget owns the accounting: every model call is admitted and charged, whatever the provider reports."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from picopg.server.ai.budget import AiBudget, AiLimited, current_principal
from picopg.server.ai.model import FallbackModel, OpenAiCompatibleChatModel


class Provider(BaseHTTPRequestHandler):
    """一个不报 usage、流式时夹着空 data 行的 provider。"""

    calls = 0

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Provider.calls += 1
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data:\n\n")
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': '好'}}]})}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        reply = json.dumps({"choices": [{"message": {"content": "{\"ok\": true}"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args: object) -> None:
        pass


class MeteringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}"
        Provider.calls = 0

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def model(self, budget: AiBudget) -> OpenAiCompatibleChatModel:
        model = OpenAiCompatibleChatModel(self.url, "m")
        model.meter = budget
        return model

    def test_every_call_is_admitted_for_the_current_person_and_estimated_without_usage(self) -> None:
        budget = AiBudget(per_hour=2)
        model = self.model(budget)
        token = current_principal.set("ana")
        try:
            model.complete([{"role": "user", "content": "x" * 300}])
            assert budget.tokens >= 100, "no usage reported: estimated from the characters, never free"
            model.step([{"role": "user", "content": "q"}], [], lambda _text: None)
            with self.assertRaises(AiLimited):
                model.complete([{"role": "user", "content": "q"}])
            assert Provider.calls == 2, "the refused call never reached the provider"
        finally:
            current_principal.reset(token)
        token = current_principal.set("bo")
        try:
            model.complete([{"role": "user", "content": "q"}])
        finally:
            current_principal.reset(token)

    def test_a_limit_is_not_worked_around_by_the_next_provider(self) -> None:
        budget = AiBudget(per_hour=1, daily_tokens=1)
        budget.spend(1)
        fallback = FallbackModel([self.model(budget), self.model(budget)])
        with self.assertRaises(AiLimited):
            fallback.complete([{"role": "user", "content": "q"}])
        assert Provider.calls == 0

    def test_an_empty_data_line_in_a_stream_is_keep_alive(self) -> None:
        pieces: list[str] = []
        message = self.model(AiBudget()).step([{"role": "user", "content": "q"}], [], pieces.append)
        assert pieces == ["好"] and message["content"] == "好"

    def test_people_who_stopped_using_ai_do_not_stay_in_the_table(self) -> None:
        now = [0.0]
        budget = AiBudget(per_hour=5, clock=lambda: now[0])
        budget.admit("ana")
        now[0] = 3600
        budget.admit("bo")
        assert set(budget.requests) == {"bo"}


if __name__ == "__main__":
    unittest.main()
