from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from picopg.replica.file_store import FileReplicaLogStore
from picopg.server.ai.course import CourseAiService, CourseSkillRepository
from picopg.server.ai.model import CourseAiError
from picopg.server.app import PicoPGHttpServer
from picopg.server.auth.model import PrincipalSession
from picopg.server.config import SKILLS


class FakeCourseAi:
    def __init__(self) -> None:
        self.requests = []

    def reply(self, playground_id, version_id, REQUEST):
        self.requests.append((playground_id, version_id, REQUEST))
        return {"title": "看循环", "text": "先看 break 停掉哪一层。"}


class SequencedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.messages = []

    def complete(self, messages):
        self.messages.append(messages)
        return next(self.replies)


def post(connection, token, body):
    connection.request(
        "POST",
        "/api/v1/ai/course",
        json.dumps(body).encode(),
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    response = connection.getresponse()
    return response.status, json.loads(response.read())


REQUEST = {
    "kind": "question",
    "text": "为什么找到解还在跑？",
    "quote": None,
    "context": {"source": "for a in range(4):\n    break", "moment": None},
}

CONTEXTUAL = {**REQUEST, "context": {
    "source": "x = 1\nprint(x)",
    "moment": {
        "step": 2, "steps": 3, "line": 2, "occurrence": None,
        "frames": [], "globals": {"x": 1}, "stdout": "",
    },
}}


class CourseAiRouteTest(unittest.TestCase):
    def test_route_requires_a_session_and_valid_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = FakeCourseAi()
            session = PrincipalSession.create("student", "device", "student-token", {}, ["playgrounds"])
            server = PicoPGHttpServer(
                ("127.0.0.1", 0),
                FileReplicaLogStore(root / "replica"),
                "operator-token",
                root / "packages",
                principal_sessions=[session],
                course_ai=fake,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(*server.server_address)
            try:
                body = {"playgroundId": "sudoku", "versionId": "v1", "request": REQUEST}
                status, reply = post(connection, "student-token", body)
                assert status == 200 and reply["text"].startswith("先看")
                assert fake.requests == [("sudoku", "v1", REQUEST)]
                status, error = post(connection, "wrong-token", body)
                assert status == 401 and error["error"]["code"] == "auth.required"
                invalid = {**body, "request": {**REQUEST, "context": {"source": "x"}}}
                status, error = post(connection, "student-token", invalid)
                assert status == 400 and error["error"]["code"] == "ai.request.invalid"
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join()


class CourseAiServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.skills = CourseSkillRepository(SKILLS)

    def test_invalid_json_is_retried_once(self) -> None:
        model = SequencedModel([
            '{"text":"请看第 99 行。"}',
            '{"text":"先看 `break` 停掉哪一层。","primary":{"action":"code.locate","args":{"line":2}}}',
        ])
        service = CourseAiService(model, self.skills)
        reply = service.reply("sudoku", "v1", REQUEST)
        assert reply["primary"]["args"] == {"line": 2}
        assert reply["primary"]["label"] == "定位这一行"
        assert len(model.messages) == 2 and "上个 JSON 无效" in model.messages[1][-1]["content"]

    def test_short_action_forms_are_completed_from_context(self) -> None:
        short_model = SequencedModel([
            '{"text":"看当前这一步。","actions":["trace.seek",{"id":"code.locate"}]}'
        ])
        short_reply = CourseAiService(short_model, self.skills).reply("sudoku", "v1", CONTEXTUAL)
        assert short_reply["actions"] == [
            {"action": "trace.seek", "label": "跳到这一步", "args": {"step": 2}},
            {"action": "code.locate", "label": "定位这一行", "args": {"line": 2}},
        ]

    def test_teaching_actions_are_validated(self) -> None:
        active_model = SequencedModel(['''{
          "text":"先把这一行标出来，再看一个只改动输出的临时版本。",
          "primary":{"action":"annotation.show","args":{"kind":"note","at":{"line":2,"text":"print"},"text":"这里读取 x"}},
          "actions":[
            {"action":"source.explain","args":{"source":"x = 1\\nprint(x + 1)"}},
            {"action":"code.run"},
            {"action":"source.restore"}
          ]
        }'''])
        active_reply = CourseAiService(active_model, self.skills).reply("sudoku", "v1", CONTEXTUAL)
        assert active_reply["primary"] == {
            "action": "annotation.show", "label": "在代码上标出来",
            "args": {"kind": "note", "at": {"line": 2, "text": "print"}, "text": "这里读取 x"},
        }
        assert [action["action"] for action in active_reply["actions"]] == [
            "source.explain", "code.run", "source.restore",
        ]

    def test_forbidden_actions_are_rejected(self) -> None:
        bad_model = SequencedModel(['{"text":"答案","primary":{"action":"source.replace","label":"替换"}}'] * 2)
        with self.assertRaises(CourseAiError, msg="forbidden actions must be rejected after one retry"):
            CourseAiService(bad_model, self.skills).reply("sudoku", "v1", REQUEST)


if __name__ == "__main__":
    unittest.main()
