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
from picopg.server.ai.access import AiAccess
from picopg.server.app import PicoPGHttpServer
from picopg.server.auth.model import PrincipalSession
from picopg.server.config import SKILLS


class RecordingModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def step(self, messages, tools, on_text=None):
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
    def __init__(self):
        self.learners = []

    def step(self, playground_id, version_id, messages, tools, on_text=None, learner=None):
        self.learners.append(learner)
        if on_text is not None:
            if playground_id == "broken":
                on_text("说到一半")
                raise CourseAiError("model request failed")
            for piece in ("先看", "**第 2 行**"):
                on_text(piece)
        return {"content": f"{playground_id}:{len(messages)}", "calls": []}

    def tutor(self, messages, tools, on_text=None, learner=None):
        self.learners.append(learner)
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
        self.assertIn("说到哪，就让学生看到哪", sent[0]["content"])
        self.assertEqual(sent[2], {
            "role": "assistant", "content": "", "reasoning_content": "先标出来",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "act", "arguments": "{\"action\":\"code.run\"}"}}],
        })
        self.assertEqual(sent[3], {"role": "tool", "tool_call_id": "c1", "content": "{\"outcome\":\"done\"}"})
        self.assertEqual(tools, [{"type": "function", "function": TOOLS[0]}])

    def test_tool_calls_written_as_text_are_recovered_and_never_streamed(self) -> None:
        # 有的模型把调用写成正文 functions.名字({...})：本轮工具里有这个名字、参数是 JSON 对象的，当作真的调用；
        # 边生成边给学生看的文字在这串字开始的地方截断。
        written = '回到第 6 行。functions.act({"action": "code.run", "args": {"at": {"line": 6}}})'

        class StreamingModel(RecordingModel):
            def step(self, messages, tools, on_text=None):
                for end in (8, 20, len(written)):
                    on_text(written[:end])
                return super().step(messages, tools, on_text)

        streamed = []
        model = StreamingModel([{"content": written}])
        turn = CourseAgentService(model, self.skills).step("sudoku", "v1", MESSAGES, TOOLS, streamed.append)
        self.assertEqual(turn["content"], "回到第 6 行。")
        self.assertEqual([(call["name"], json.loads(call["arguments"])) for call in turn["calls"]],
                         [("act", {"action": "code.run", "args": {"at": {"line": 6}}})])
        self.assertTrue(all("functions" not in text for text in streamed), streamed)
        unknown = CourseAgentService(RecordingModel([{"content": 'functions.nope({"a": 1})'}]), self.skills).step("sudoku", "v1", MESSAGES, TOOLS)
        self.assertEqual((unknown["content"], unknown["calls"]), ('functions.nope({"a": 1})', []), "a name that is not a tool stays text")

    def test_teaching_skills_load_by_tools_and_what_the_student_said(self) -> None:
        # core 总在；练习与批改随工具出现；笔记只在学生要整理时加载；会变的 skill 排在本课要点之后。
        service = CourseAgentService(RecordingModel([]), self.skills)
        tool = lambda name: {"name": name, "description": "", "parameters": {}}  # noqa: E731
        plain = service.system("sudoku", "v1", MESSAGES, [tool("ask_to_write")])
        self.assertIn("## 提问与练习", plain)
        self.assertNotIn("## 批改交回的练习", plain)
        self.assertNotIn("## 整理知识点", plain)
        grading = service.system("sudoku", "v1", MESSAGES, [tool("ask_to_write"), tool("grade")])
        self.assertTrue(grading.startswith(plain))
        self.assertIn("misconception", grading)
        answering = service.system("sudoku", "v1", MESSAGES, [tool("ask_to_write"), tool("assess")])
        self.assertIn("## 判定学生的回答", answering)
        self.assertNotIn("## 判定学生的回答", grading)
        asked = [{"role": "user", "content": "学生：帮我整理一下今天的知识点\n\n编辑器里的代码：\n1| x = 1"}]
        self.assertIn("## 整理知识点", service.system("sudoku", "v1", asked, []))
        # 代码里出现的词不算学生说的话。
        code = [{"role": "user", "content": "学生：这行什么意思\n\n编辑器里的代码：\n1| # 复习"}]
        self.assertNotIn("## 整理知识点", service.system("sudoku", "v1", code, []))

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

    def test_the_learner_profile_goes_last_in_the_system_prompt(self) -> None:
        model = RecordingModel([{"content": "好"}, {"content": "好"}])
        service = CourseAgentService(model, self.skills)
        service.step("sudoku", "v1", MESSAGES[:1], [], learner={"concepts": [{"concept": "for", "status": "shaky"}]})
        service.step("sudoku", "v1", MESSAGES[:1], [])
        with_profile, without = (call[0][0]["content"] for call in model.calls)
        self.assertTrue(with_profile.startswith(without), "the stable prefix is unchanged")
        self.assertIn("学生画像（JSON；用来决定教什么、问什么，不要念给学生听）", with_profile)
        self.assertIn("askedBeforeSubmit：交练习前平均问 AI 几次（低说明先自己试，是好习惯）", with_profile, "the fields are explained")
        self.assertTrue(with_profile.endswith('\n{"concepts":[{"concept":"for","status":"shaky"}]}'))
        self.assertNotIn("学生画像", without)

    def test_a_comment_draft_reads_the_code_lesson_and_profile_without_tools(self) -> None:
        model = RecordingModel([{"content": "  **做对了**：第 1 行。  "}])
        text = CourseAgentService(model, self.skills).draft_comment("sudoku", "v1", "a = 1\nprint(a)\n", {"concepts": []})
        self.assertEqual(text, "**做对了**：第 1 行。")
        sent, tools = model.calls[0]
        self.assertEqual(tools, [])
        self.assertTrue(sent[0]["content"].startswith("你在帮初中 Python 老师起草"))
        self.assertIn("本课的讲解要点", sent[0]["content"])
        self.assertIn("学生画像", sent[0]["content"])
        self.assertEqual(sent[1]["content"], "学生提交的代码：\n1| a = 1\n2| print(a)")

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
            agent = FakeAgent()
            server = PicoPGHttpServer(
                ("127.0.0.1", 0), FileReplicaLogStore(root / "replica"), "operator-token", root / "packages",
                principal_sessions=[session], course_agent=agent, ai_access=AiAccess(["student"]),
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
                # 服务端按提问的学生算出画像：课程里只给本课相关的部分，导师拿到全部与到期复习。
                course, tutor = agent.learners
                self.assertEqual(set(course), {"concepts", "misconceptions", "habits", "due", "open"})
                self.assertEqual((tutor["version"], tutor["due"]), (1, []))
                status, error = post(connection, "wrong-token", body)
                self.assertEqual((status, error["error"]["code"]), (401, "auth.required"))
                status, error = post(connection, "student-token", {**body, "tools": "act"})
                self.assertEqual((status, error["error"]["code"]), (400, "ai.request.invalid"))
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join()

    def test_streamed_steps_send_text_then_the_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = PrincipalSession.create("student", "device", "student-token", {}, ["playgrounds"])
            server = PicoPGHttpServer(
                ("127.0.0.1", 0), FileReplicaLogStore(root / "replica"), "operator-token", root / "packages",
                principal_sessions=[session], course_agent=FakeAgent(), ai_access=AiAccess(["student"]),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def stream(playground_id):
                connection = http.client.HTTPConnection(*server.server_address)
                body = {"playgroundId": playground_id, "versionId": "v1", "messages": MESSAGES, "tools": TOOLS}
                connection.request("POST", "/api/v1/ai/agent", json.dumps(body).encode(), {
                    "Authorization": "Bearer student-token", "Content-Type": "application/json", "Accept": "application/x-ndjson",
                })
                response = connection.getresponse()
                lines = [json.loads(line) for line in response.read().decode().splitlines()]
                connection.close()
                return response, lines

            try:
                response, lines = stream("hello")
                self.assertEqual(response.getheader("X-Accel-Buffering"), "no")
                self.assertEqual(lines, [{"text": "先看"}, {"text": "**第 2 行**"}, {"turn": {"content": "hello:3", "calls": []}}])
                _, lines = stream("broken")
                self.assertEqual(lines, [{"text": "说到一半"}, {"error": {"code": "ai.reply.invalid", "message": "model request failed"}}])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
