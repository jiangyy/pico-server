from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from picopg.server.ai.model import CourseAiError, OpenAiCompatibleChatModel

CHUNKS = [
    {"choices": [{"delta": {"reasoning_content": "想一想"}}]},
    {"choices": [{"delta": {"content": "先看"}}]},
    {"choices": [{"delta": {"content": "**第 2 行**"}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "act", "arguments": "{\"act"}}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "ion\":\"code.run\"}"}}]}}]},
    {"choices": [], "usage": {"total_tokens": 42}},
]


class Upstream(BaseHTTPRequestHandler):
    bodies: list[dict[str, object]] = []

    def do_POST(self) -> None:
        Upstream.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in CHUNKS:
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *args: object) -> None:
        pass


class StreamingModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), Upstream)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.model = OpenAiCompatibleChatModel(f"http://{host}:{port}", "m")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_text_arrives_in_pieces_and_tool_calls_are_assembled(self) -> None:
        pieces, spent = [], []
        self.model.on_usage = spent.append
        message = self.model.step([{"role": "user", "content": "hi"}], [], pieces.append)
        assert Upstream.bodies[-1]["stream"] is True
        assert pieces == ["先看", "**第 2 行**"]
        assert message == {
            "role": "assistant", "content": "先看**第 2 行**", "reasoning_content": "想一想",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "act", "arguments": "{\"action\":\"code.run\"}"}}],
        }
        assert spent == [42]

    def test_a_client_that_goes_away_is_not_a_model_failure(self) -> None:
        def gone(_: str) -> None:
            raise BrokenPipeError()
        with self.assertRaises(BrokenPipeError):
            self.model.step([{"role": "user", "content": "hi"}], [], gone)

    def test_an_unreachable_model_is_a_model_failure(self) -> None:
        with self.assertRaises(CourseAiError):
            OpenAiCompatibleChatModel("http://127.0.0.1:9", "m").step([], [], lambda _: None)
