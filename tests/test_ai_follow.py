from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from picopg.replica.file_store import FileReplicaLogStore
from picopg.server.ai.access import AiAccess
from picopg.server.ai.course import OneSkill
from picopg.server.ai.follow import VERSION, CourseFollowService, follow_request
from picopg.server.ai.model import CourseAiError
from picopg.server.app import PicoPGHttpServer
from picopg.server.auth.model import PrincipalSession

SKILL = {"course": "hanoi", "teaches": "把搬盘子拆成更小的同类任务。", "concepts": ["recursion"], "rules": "只能顺时针搬。"}
NOTE = {
    "doing": "在补 else", "belief": {"text": "认为问题在第 22、23 行", "lines": [22, 23], "steps": [6]},
    "question": "为什么红叉还在第 6 步", "gap": "execution", "next": {"action": "edit", "lines": [22]},
    "want": {"say": "wait", "why": "他还在试"}, "basis": [{"attempt": 7, "line": 22}],
    "lacks": [{"what": "递归调用 hanoi(k, X, Y) 的意思", "seen": ["hanoi(n - 1, src, dst)"]}],
}
REQUEST = {"reference": "def hanoi(n, src, dst): ...", "thread": [{"role": "user", "content": "第 1 次尝试"}]}


class RecordingModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def step(self, messages, tools, on_text=None):
        self.calls.append([dict(message) for message in messages])
        return {"content": next(self.replies)}


class CourseFollowServiceTest(unittest.TestCase):
    def test_course_prefix_then_the_thread(self) -> None:
        model = RecordingModel([json.dumps(NOTE, ensure_ascii=False)])
        thread = [{"role": "user", "content": "第 1 次"}, {"role": "assistant", "content": "{}"}, {"role": "user", "content": "第 2 次"}]
        note = CourseFollowService(model, OneSkill(SKILL)).follow("hanoi", "v1", {**REQUEST, "thread": thread})
        self.assertEqual(note, {**NOTE, "version": VERSION})
        system, *rest = model.calls[0]
        self.assertIn("## 题目\n把搬盘子拆成更小的同类任务。", system["content"])
        self.assertIn("def hanoi(n, src, dst)", system["content"])
        self.assertEqual([turn["content"] for turn in rest], ["第 1 次", "{}", "第 2 次"])

    def test_a_malformed_note_is_sent_back_once(self) -> None:
        model = RecordingModel([json.dumps({**NOTE, "gap": "unknown"}), json.dumps(NOTE, ensure_ascii=False)])
        self.assertEqual(CourseFollowService(model, OneSkill(SKILL)).follow("hanoi", "v1", REQUEST)["gap"], "execution")
        self.assertIn("gap 只能是", model.calls[1][-1]["content"])
        with self.assertRaises(CourseAiError):
            CourseFollowService(RecordingModel(["no", "still no"]), OneSkill(SKILL)).follow("hanoi", "v1", REQUEST)

    def test_lacks_must_name_a_piece_and_show_his_code(self) -> None:
        model = RecordingModel([json.dumps({**NOTE, "lacks": ["for 循环"]}, ensure_ascii=False), json.dumps(NOTE, ensure_ascii=False)])
        note = CourseFollowService(model, OneSkill(SKILL)).follow("hanoi", "v1", REQUEST)
        self.assertEqual(note["lacks"], NOTE["lacks"])
        self.assertIn("lacks", model.calls[-1][-1]["content"])

    def test_the_thread_must_alternate_and_end_with_the_student(self) -> None:
        body = {"playgroundId": "hanoi", "versionId": "v1", **REQUEST}
        self.assertEqual(follow_request(body)[2], REQUEST)
        for thread in ([], [{"role": "assistant", "content": "x"}], [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]):
            with self.assertRaises(ValueError):
                follow_request({**body, "thread": thread})


class FakeFollow:
    def follow(self, playground_id, version_id, request):
        return {**NOTE, "doing": f"{playground_id}:{len(request['thread'])}", "version": VERSION}


class FollowRouteTest(unittest.TestCase):
    def test_follow_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = PrincipalSession.create("student", "device", "student-token", {}, ["playgrounds"])
            server = PicoPGHttpServer(
                ("127.0.0.1", 0), FileReplicaLogStore(root / "replica"), "operator-token", root / "packages",
                principal_sessions=[session], ai_access=AiAccess(["student"]), course_follow=FakeFollow(),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def post(token, value):
                connection = http.client.HTTPConnection(*server.server_address)
                try:
                    connection.request("POST", "/api/v1/ai/follow", json.dumps(value).encode(), {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()

            try:
                body = {"playgroundId": "hanoi", "versionId": "v1", **REQUEST}
                self.assertEqual(post("student-token", body), (200, {**NOTE, "doing": "hanoi:1", "version": VERSION}))
                self.assertEqual(post("wrong-token", body)[0], 401)
                self.assertEqual(post("student-token", {**body, "thread": []})[0], 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
