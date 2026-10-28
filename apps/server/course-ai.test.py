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

from apps.server.application import PicoPGHttpServer  # noqa: E402
from apps.server.authentication.model import PrincipalSession  # noqa: E402
from apps.server.course_ai.service import (  # noqa: E402
    CourseAiError,
    CourseAiService,
    CourseSkillRepository,
)
from picopg.impl.file_replica_log import FileReplicaLogStore  # noqa: E402


class FakeCourseAi:
    def __init__(self) -> None:
        self.requests = []

    def reply(self, playground_id, version_id, request):
        self.requests.append((playground_id, version_id, request))
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


request = {
    "kind": "question",
    "text": "为什么找到解还在跑？",
    "quote": None,
    "context": {"source": "for a in range(4):\n    break", "moment": None},
}

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
    body = {"playgroundId": "sudoku", "versionId": "v1", "request": request}
    status, reply = post(connection, "student-token", body)
    assert status == 200 and reply["text"].startswith("先看")
    assert fake.requests == [("sudoku", "v1", request)]
    status, error = post(connection, "wrong-token", body)
    assert status == 401 and error["error"]["code"] == "auth.required"
    invalid = {**body, "request": {**request, "context": {"source": "x"}}}
    status, error = post(connection, "student-token", invalid)
    assert status == 400 and error["error"]["code"] == "ai.request.invalid"
    connection.close()
    server.shutdown()
    server.server_close()
    thread.join()

skills = CourseSkillRepository(Path(__file__).parent / "course_ai" / "skills")
model = SequencedModel([
    '{"text":"请看第 99 行。"}',
    '{"text":"先看 `break` 停掉哪一层。","primary":{"action":"code.locate","args":{"line":2}}}',
])
service = CourseAiService(model, skills)
reply = service.reply("sudoku", "v1", request)
assert reply["primary"]["args"] == {"line": 2}
assert reply["primary"]["label"] == "定位这一行"
assert len(model.messages) == 2 and "上个 JSON 无效" in model.messages[1][-1]["content"]

contextual = {**request, "context": {
    "source": "x = 1\nprint(x)",
    "moment": {
        "step": 2, "steps": 3, "line": 2, "occurrence": None,
        "frames": [], "globals": {"x": 1}, "stdout": "",
    },
}}
short_model = SequencedModel([
    '{"text":"看当前这一步。","actions":["trace.seek",{"id":"code.locate"}]}'
])
short_reply = CourseAiService(short_model, skills).reply("sudoku", "v1", contextual)
assert short_reply["actions"] == [
    {"action": "trace.seek", "label": "跳到这一步", "args": {"step": 2}},
    {"action": "code.locate", "label": "定位这一行", "args": {"line": 2}},
]

bad_model = SequencedModel(['{"text":"答案","primary":{"action":"source.replace","label":"替换"}}'] * 2)
try:
    CourseAiService(bad_model, skills).reply("sudoku", "v1", request)
except CourseAiError:
    pass
else:
    raise AssertionError("forbidden actions must be rejected after one retry")

print("authenticated course AI route, retry and action validation passed")
