from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from picopg.replica.file_store import FileReplicaLogStore
from picopg.server.ai.access import AiAccess
from picopg.server.ai.step import PROMPTS, step_request
from picopg.server.app import PicoPGHttpServer
from picopg.server.auth.model import PrincipalSession

COURSE = {"playgroundId": "hanoi", "versionId": "v1"}
THREAD = [{"role": "user", "content": "第 1 次尝试"}]
MESSAGES = [{"role": "user", "content": "为什么不对"}]
TOOL = {"name": "read", "description": "读事实", "parameters": {"type": "object"}}


class FakeFollow:
    def follow(self, playground_id, version_id, request):
        return {"doing": f"{playground_id}:{version_id}:{len(request['thread'])}"}


class FakeHint:
    def hint(self, playground_id, version_id, evidence, focus):
        return {"reason": f"{evidence}:{focus}", "problems": []}


class FakeAgent:
    def __init__(self):
        self.learners = []

    def step(self, playground_id, version_id, messages, tools, on_text=None, learner=None):
        if on_text is not None:
            on_text("先")
        return {"content": f"{playground_id}:{len(messages)}:{[tool['name'] for tool in tools]}", "calls": []}

    def tutor(self, messages, tools, on_text=None, learner=None):
        return {"content": f"tutor:{len(messages)}", "calls": []}


class StepRequestTest(unittest.TestCase):
    def test_every_prompt_is_named_by_its_teacher_and_purpose(self) -> None:
        self.assertEqual(sorted(PROMPTS), ["coach.debrief", "coach.diagnose", "coach.follow", "coach.hint", "course.agent", "pico.tutor"])

    def test_the_envelope_and_the_input_are_both_checked(self) -> None:
        name, _, course, parsed = step_request({"prompt": "coach.follow", "course": COURSE, "input": {"reference": None, "thread": THREAD}})
        self.assertEqual((name, course.playground_id, parsed["thread"]), ("coach.follow", "hanoi", THREAD))  # type: ignore[union-attr, index]
        for body in (
            {"prompt": "coach.follow", "course": COURSE},
            {"prompt": "coach.unknown", "course": COURSE, "input": {}},
            {"prompt": "coach.follow", "course": None, "input": {"reference": None, "thread": THREAD}},
            {"prompt": "coach.follow", "course": {**COURSE, "versionId": ""}, "input": {"reference": None, "thread": THREAD}},
            {"prompt": "coach.follow", "course": COURSE, "input": {"reference": None, "thread": []}},
            {"prompt": "coach.hint", "course": COURSE, "input": {"evidence": "x"}},
        ):
            with self.assertRaises(ValueError):
                step_request(body)

    def test_the_tutor_works_outside_any_course(self) -> None:
        _, prompt, course, parsed = step_request({"prompt": "pico.tutor", "course": None, "input": {"messages": MESSAGES, "tools": []}})
        self.assertEqual((prompt.tools, course, parsed), (True, None, (MESSAGES, [])))


class StepRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        session = PrincipalSession.create("student", "device", "student-token", {}, ["playgrounds"])
        self.server = PicoPGHttpServer(
            ("127.0.0.1", 0), FileReplicaLogStore(root / "replica"), "operator-token", root / "packages",
            principal_sessions=[session], ai_access=AiAccess(["student"]),
            course_follow=FakeFollow(), course_hint=FakeHint(), course_agent=FakeAgent(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def post(self, value, token="student-token", accept="application/json"):
        connection = http.client.HTTPConnection(*self.server.server_address)
        try:
            connection.request("POST", "/api/v1/ai/step", json.dumps(value).encode(),
                               {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": accept})
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def test_a_written_answer_comes_back_as_the_turn_value(self) -> None:
        status, body = self.post({"prompt": "coach.follow", "course": COURSE, "input": {"reference": None, "thread": THREAD}})
        self.assertEqual((status, json.loads(body)), (200, {"turn": {"content": "", "calls": [], "value": {"doing": "hanoi:v1:1"}}}))
        status, body = self.post({"prompt": "coach.hint", "course": COURSE, "input": {"evidence": "e", "focus": "hanoi"}})
        self.assertEqual(json.loads(body)["turn"]["value"], {"reason": "e:hanoi", "problems": []})

    def test_a_tool_prompt_is_one_step_and_can_stream(self) -> None:
        status, body = self.post({"prompt": "course.agent", "course": COURSE, "input": {"messages": MESSAGES, "tools": [TOOL]}})
        self.assertEqual((status, json.loads(body)["turn"]["content"]), (200, "hanoi:1:['read']"))
        status, body = self.post({"prompt": "course.agent", "course": COURSE, "input": {"messages": MESSAGES, "tools": []}}, accept="application/x-ndjson")
        self.assertEqual([json.loads(line) for line in body.splitlines()], [{"text": "先"}, {"turn": {"content": "hanoi:1:[]", "calls": []}}])
        status, body = self.post({"prompt": "pico.tutor", "course": None, "input": {"messages": MESSAGES, "tools": []}})
        self.assertEqual(json.loads(body)["turn"]["content"], "tutor:1")

    def test_refusals(self) -> None:
        follow = {"prompt": "coach.follow", "course": COURSE, "input": {"reference": None, "thread": THREAD}}
        self.assertEqual(self.post(follow, token="wrong")[0], 401)
        self.assertEqual(self.post({**follow, "prompt": "nope"})[0], 400)
        self.assertEqual(self.post({"prompt": "coach.debrief", "course": COURSE, "input": {"source": "x", "facts": "y"}})[0], 503)


if __name__ == "__main__":
    unittest.main()
