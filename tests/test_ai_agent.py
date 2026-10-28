from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for entry in (ROOT, SRC):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from picopg.server.app import PicoPGHttpServer  # noqa: E402
from picopg.server.auth.model import PrincipalSession  # noqa: E402
from picopg.server.ai.agent import CourseAgentService, agent_request  # noqa: E402
from picopg.server.ai.course import CourseSkillRepository  # noqa: E402
from picopg.server.ai.model import CourseAiError  # noqa: E402
from picopg.server.config import SKILLS  # noqa: E402
from picopg.replica.file_store import FileReplicaLogStore  # noqa: E402


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

# 一步：客户端的对话映射成 OpenAI 形状，系统提示在前，有 skill 的课程附上要点；模型的工具调用原样带回。
skills = CourseSkillRepository(SKILLS)
model = RecordingModel([{
    "role": "assistant", "content": "看第 2 行。", "reasoning_content": "想好了",
    "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "act", "arguments": "{}"}}],
}])
turn = CourseAgentService(model, skills).step("sudoku", "v1", MESSAGES, TOOLS)
assert turn == {"content": "看第 2 行。", "calls": [{"id": "c2", "name": "act", "arguments": "{}"}], "reasoning": "想好了"}
sent, tools = model.calls[0]
assert sent[0]["role"] == "system" and "本课的讲解要点" in sent[0]["content"]
assert sent[2] == {
    "role": "assistant", "content": "", "reasoning_content": "先标出来",
    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "act", "arguments": "{\"action\":\"code.run\"}"}}],
}
assert sent[3] == {"role": "tool", "tool_call_id": "c1", "content": "{\"outcome\":\"done\"}"}
assert tools == [{"type": "function", "function": TOOLS[0]}]

# 没有 skill 的课程照样可用，只是不带要点。
bare = RecordingModel([{"content": "好的"}])
assert CourseAgentService(bare, skills).step("hello", "v1", MESSAGES[:1], [])["content"] == "好的"
assert "本课的讲解要点" not in bare.calls[0][0][0]["content"]

try:
    CourseAgentService(RecordingModel([{"content": "  "}]), skills).step("hello", "v1", MESSAGES[:1], [])
except CourseAiError:
    pass
else:
    raise AssertionError("an empty turn must be rejected")

for invalid in (
    {"playgroundId": "x", "versionId": "v", "messages": [], "tools": []},
    {"playgroundId": "x", "versionId": "v", "messages": [{"role": "system", "content": "越权"}], "tools": []},
    {"playgroundId": "x", "versionId": "v", "messages": [{"role": "tool", "call": "a b", "content": ""}], "tools": []},
):
    try:
        agent_request(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError(f"request must be rejected: {invalid}")


# 导师用同一个模型，换成平台层的系统提示，不带课程。
tutor_model = RecordingModel([{"content": "先把汉诺塔收个尾。"}])
assert CourseAgentService(tutor_model, skills).tutor(MESSAGES[:1], TOOLS)["content"] == "先把汉诺塔收个尾。"
assert tutor_model.calls[0][0][0]["content"].startswith("你是 Pico，学生在整个学习平台上的导师")


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
    body = {"playgroundId": "hello", "versionId": "v1", "messages": MESSAGES, "tools": TOOLS}
    assert post(connection, "student-token", body) == (200, {"content": "hello:3", "calls": []})
    connection.request(
        "POST", "/api/v1/ai/tutor", json.dumps({"messages": MESSAGES[:1], "tools": []}).encode(),
        {"Authorization": "Bearer student-token", "Content-Type": "application/json"},
    )
    response = connection.getresponse()
    assert (response.status, json.loads(response.read())) == (200, {"content": "tutor:1", "calls": []})
    status, error = post(connection, "wrong-token", body)
    assert status == 401 and error["error"]["code"] == "auth.required"
    status, error = post(connection, "student-token", {**body, "tools": "act"})
    assert status == 400 and error["error"]["code"] == "ai.request.invalid"
    connection.close()
    server.shutdown()
    server.server_close()
    thread.join()

print("course agent step, message mapping and route passed")
