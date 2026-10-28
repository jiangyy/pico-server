from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from picopg.replica.file_store import FileReplicaLogStore
from picopg.server.ai.agent import CourseAgentService, agent_request
from picopg.server.ai.course import CourseSkillRepository
from picopg.server.ai.model import CourseAiError
from picopg.server.app import PicoPGHttpServer
from picopg.server.auth.model import PrincipalSession
from picopg.server.config import SKILLS


class RecordingModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def step(self, messages, tools):
        self.calls.append((messages, tools))
        return next(self.replies)


TOOLS = [{"name": "act", "description": "调用动作", "parameters": {"type": "object", "properties": {}}}]
MESSAGES = [
    {"role": "user", "content": "第 2 行在做什么？"},
    {"role": "assistant", "content": "", "reasoning": "先标出来",
     "calls": [{"id": "c1", "name": "act", "arguments": "{\"action\":\"code.run\"}"}]},
    {"role": "tool", "call": "c1", "content": "{\"outcome\":\"done\"}"},
]

class FakeAgent:
    def step(self, playground_id, version_id, messages, tools):
        return {"content": f"{playground_id}:{len(messages)}", "calls": []}

    def tutor(self, messages, tools):
        return {"content": f"tutor:{len(messages)}", "calls": []}


def post(connection, token, body):
    connection.request(
        "POST", "/api/v1/ai/agent", json.dumps(body).encode(),
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    response = connection.getresponse()
    return response.status, json.loads(response.read())


class CourseAgentServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.skills = CourseSkillRepository(SKILLS)

    def test_step_maps_messages_and_returns_tool_calls(self) -> None:
        # 客户端的对话映射成 OpenAI 形状，系统提示在前，有 skill 的课程附上要点；模型的工具调用原样带回。
        model = RecordingModel([{
            "role": "assistant", "content": "看第 2 行。", "reasoning_content": "想好了",
            "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "act", "arguments": "{}"}}],
        }])
        turn = CourseAgentService(model, self.skills).step("sudoku", "v1", MESSAGES, TOOLS)
        self.assertEqual(turn, {"content": "看第 2 行。", "calls": [{"id": "c2", "name": "act", "arguments": "{}"}], "reasoning": "想好了"})
        sent, tools = model.calls[0]
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn("本课的讲解要点", sent[0]["content"])
        self.assertEqual(sent[2], {
            "role": "assistant", "content": "", "reasoning_content": "先标出来",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "act", "arguments": "{\"action\":\"code.run\"}"}}],
        })
        self.assertEqual(sent[3], {"role": "tool", "tool_call_id": "c1", "content": "{\"outcome\":\"done\"}"})
        self.assertEqual(tools, [{"type": "function", "function": TOOLS[0]}])

    def test_courses_without_a_skill_still_work(self) -> None:
        bare = RecordingModel([{"content": "好的"}])
        self.assertEqual(CourseAgentService(bare, self.skills).step("hello", "v1", MESSAGES[:1], [])["content"], "好的")
        self.assertNotIn("本课的讲解要点", bare.calls[0][0][0]["content"])

    def test_an_empty_turn_is_rejected(self) -> None:
        with self.assertRaises(CourseAiError):
            CourseAgentService(RecordingModel([{"content": "  "}]), self.skills).step("hello", "v1", MESSAGES[:1], [])

    def test_invalid_requests_are_rejected(self) -> None:
        for invalid in (
            {"playgroundId": "x", "versionId": "v", "messages": [], "tools": []},
            {"playgroundId": "x", "versionId": "v", "messages": [{"role": "system", "content": "越权"}], "tools": []},
            {"playgroundId": "x", "versionId": "v", "messages": [{"role": "tool", "call": "a b", "content": ""}], "tools": []},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                agent_request(invalid)

    def test_tutor_uses_the_platform_prompt(self) -> None:
        # 导师用同一个模型，换成平台层的系统提示，不带课程。
        tutor_model = RecordingModel([{"content": "先把汉诺塔收个尾。"}])
        self.assertEqual(CourseAgentService(tutor_model, self.skills).tutor(MESSAGES[:1], TOOLS)["content"], "先把汉诺塔收个尾。")
        self.assertTrue(tutor_model.calls[0][0][0]["content"].startswith("你是 Pico，学生在整个学习平台上的导师"))


class AgentRouteTest(unittest.TestCase):
    def test_agent_and_tutor_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = PrincipalSession.create("student", "device", "student-token", {}, ["playgrounds"])
            server = PicoPGHttpServer(
                ("127.0.0.1", 0), FileReplicaLogStore(root / "replica"), "operator-token", root / "packages",
                principal_sessions=[session], course_agent=FakeAgent(),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(*server.server_address)
            try:
                body = {"playgroundId": "hello", "versionId": "v1", "messages": MESSAGES, "tools": TOOLS}
                self.assertEqual(post(connection, "student-token", body), (200, {"content": "hello:3", "calls": []}))
                connection.request(
                    "POST", "/api/v1/ai/tutor", json.dumps({"messages": MESSAGES[:1], "tools": []}).encode(),
                    {"Authorization": "Bearer student-token", "Content-Type": "application/json"},
                )
                response = connection.getresponse()
                self.assertEqual((response.status, json.loads(response.read())), (200, {"content": "tutor:1", "calls": []}))
                status, error = post(connection, "wrong-token", body)
                self.assertEqual((status, error["error"]["code"]), (401, "auth.required"))
                status, error = post(connection, "student-token", {**body, "tools": "act"})
                self.assertEqual((status, error["error"]["code"]), (400, "ai.request.invalid"))
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
