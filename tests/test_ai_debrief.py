from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from picopg.replica.file_store import FileReplicaLogStore
from picopg.server.ai.access import AiAccess
from picopg.server.app import PicoPGHttpServer
from picopg.server.auth.model import PrincipalSession
from picopg.server.ai.course import OneSkill
from picopg.server.ai.model import CourseAiError
from picopg.server.ai.debrief import VERSION, CourseDebriefService, debrief_request
from tests.step_route import STEP, answered, envelope

SKILL = {"course": "water-container", "rules": "同一个下标不能用两次。"}
REVIEW = {
    "idea": "每个 i 当矮边，找最远的不比它矮的 k", "right": "水量只由矮的那根决定", "cleaner": "```python\ndef max_area(height):\n    return 0\n```",
    "cleaner_why": "合成一段", "math": "max min(h_i,h_j)(j-i)", "better": "按高度从高到低", "better_code": None,
    "complexity": "O(n²) → O(n log n)", "edges": "等高也对",
    "lectures": [{"title": "从高到低", "steps": [
        {"title": "哪里在重复", "beats": [{"say": "每根线都重新扫一遍。"}]},
        {"title": "存起来", "beats": [{"say": "高的先处理。"}, {"say": "先排序。", "write": "```python\ndef max_area(height):\n    return 0\n```"}]},
    ]}],
}
REQUEST = {"source": "def max_area(height):\n    return 0\n", "facts": "对拍：208 组全部一致", "reference": "def max_area(height): ..."}


class RecordingModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def step(self, messages, tools, on_text=None):
        self.calls.append([dict(message) for message in messages])
        return {"content": next(self.replies)}


class CourseDebriefServiceTest(unittest.TestCase):
    def test_edges_may_be_left_empty(self) -> None:
        model = RecordingModel([json.dumps({**REVIEW, "edges": ""}, ensure_ascii=False)])
        debrief = CourseDebriefService(model, OneSkill(SKILL)).debrief("water-container", "v1", REQUEST)
        self.assertEqual(debrief["edges"], "")
        self.assertEqual(len(model.calls), 1)

    def test_code_comes_back_without_fences(self) -> None:
        model = RecordingModel([json.dumps(REVIEW, ensure_ascii=False)])
        debrief = CourseDebriefService(model, OneSkill(SKILL)).debrief("water-container", "v1", REQUEST)
        self.assertEqual(debrief["cleaner"], "def max_area(height):\n    return 0\n")
        self.assertIsNone(debrief["better_code"])
        self.assertEqual(debrief["version"], VERSION)
        user = model.calls[0][1]["content"]
        self.assertIn("## 课程规则\n同一个下标不能用两次。", user)
        self.assertIn("对拍：208 组全部一致", user)

    def test_lectures_end_with_the_whole_solution(self) -> None:
        model = RecordingModel([json.dumps(REVIEW, ensure_ascii=False)])
        debrief = CourseDebriefService(model, OneSkill(SKILL)).debrief("water-container", "v1", REQUEST)
        self.assertEqual(debrief["lectures"][0]["steps"][1]["beats"][1]["write"], "def max_area(height):\n    return 0\n")
        self.assertNotIn("write", debrief["lectures"][0]["steps"][0]["beats"][0])
        # 写完代码再补一句解释：代码没变，照样算数（完整解法由客户端跑用例验）。
        trailing = {**REVIEW, "lectures": [{"title": "t", "steps": [{"title": "s", "beats": [
            {"say": "先排序。", "write": "```python\ndef max_area(height):\n    return 0\n```"},
            {"say": "这样就完成了。"},
        ]}]}]}
        model = RecordingModel([json.dumps(trailing, ensure_ascii=False)])
        debrief = CourseDebriefService(model, OneSkill(SKILL)).debrief("water-container", "v1", REQUEST)
        self.assertEqual(len(model.calls), 1, "写完代码后的解释不该被退回")
        self.assertEqual(debrief["lectures"][0]["steps"][0]["beats"][0]["write"], "def max_area(height):\n    return 0\n")
        unfinished = {**REVIEW, "lectures": [{"title": "t", "steps": [{"title": "s", "beats": [{"say": "只讲不写。"}]}]}]}
        model = RecordingModel([json.dumps(unfinished, ensure_ascii=False), json.dumps(REVIEW, ensure_ascii=False)])
        CourseDebriefService(model, OneSkill(SKILL)).debrief("water-container", "v1", REQUEST)
        self.assertIn("要写出代码（write）", model.calls[1][-1]["content"])

    def test_a_lecture_that_writes_too_much_at_once_is_sent_back_once(self) -> None:
        long = "def max_area(height):\n" + "".join(f"    x{k} = {k}\n" for k in range(7)) + "    return 0\n"
        rushed = {**REVIEW, "lectures": [{"title": "t", "steps": [{"title": "一口气", "beats": [{"say": "全写了。", "write": long}]}]}]}
        model = RecordingModel([json.dumps(rushed, ensure_ascii=False), json.dumps(rushed, ensure_ascii=False)])
        debrief = CourseDebriefService(model, OneSkill(SKILL)).debrief("water-container", "v1", REQUEST)
        self.assertIn("一句写出了 7 行新代码", model.calls[1][-1]["content"])
        self.assertIn("只有一句就写了代码", model.calls[1][-1]["content"])
        self.assertEqual(len(model.calls), 2, "第二次仍然太快也照样交出")
        self.assertEqual(debrief["lectures"][0]["steps"][0]["beats"][0]["write"], long)

    def test_a_missing_field_is_sent_back_once(self) -> None:
        missing = {key: value for key, value in REVIEW.items() if key != "math"}
        model = RecordingModel([json.dumps(missing, ensure_ascii=False), json.dumps(REVIEW, ensure_ascii=False)])
        CourseDebriefService(model, OneSkill(SKILL)).debrief("water-container", "v1", REQUEST)
        self.assertIn("缺少 math", model.calls[1][-1]["content"])
        model = RecordingModel([json.dumps(missing, ensure_ascii=False)] * 2)
        with self.assertRaises(CourseAiError):
            CourseDebriefService(model, OneSkill(SKILL)).debrief("water-container", "v1", REQUEST)

    def test_code_that_failed_its_run_is_sent_back(self) -> None:
        model = RecordingModel([json.dumps(REVIEW, ensure_ascii=False)])
        rejected = {"debrief": REVIEW, "result": "cleaner：对拍不一致：输入 [5, 5] 得 0，应该 5"}
        CourseDebriefService(model, OneSkill(SKILL)).debrief("water-container", "v1", {**REQUEST, "rejected": rejected})
        self.assertIn("[5, 5] 得 0", model.calls[0][-1]["content"])

    def test_request_shape(self) -> None:
        body = {"playgroundId": "water-container", "versionId": "v1", **REQUEST}
        self.assertEqual(debrief_request(body)[2]["facts"], REQUEST["facts"])
        with self.assertRaises(ValueError):
            debrief_request({**body, "extra": 1})
        with self.assertRaises(ValueError):
            debrief_request({**body, "rejected": {"debrief": REVIEW}})


class FakeDebrief:
    def __init__(self) -> None:
        self.fail = False

    def debrief(self, playground_id, version_id, request):
        if self.fail:
            raise CourseAiError("the model wrote code that does not run")
        return {"course": playground_id, "version": version_id, "source": request["source"]}


class DebriefRouteTest(unittest.TestCase):
    def test_debrief_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = FakeDebrief()
            sessions = [
                PrincipalSession.create("student", "device", "student-token", {}, ["playgrounds"]),
                PrincipalSession.create("other", "device", "other-token", {}, ["playgrounds"]),
            ]
            server = PicoPGHttpServer(
                ("127.0.0.1", 0), FileReplicaLogStore(root / "replica"), "operator-token", root / "packages",
                principal_sessions=sessions, ai_access=AiAccess(["student"]), course_debrief=fake,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def call(token, value, method="POST"):
                connection = http.client.HTTPConnection(*server.server_address)
                try:
                    connection.request(method, STEP, json.dumps(envelope("coach.debrief", value)).encode(), {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                    response = connection.getresponse()
                    return answered(response.status, json.loads(response.read()))
                finally:
                    connection.close()

            try:
                body = {"playgroundId": "hanoi", "versionId": "v1", "source": "print(1)", "facts": "n=3"}
                self.assertEqual(call("student-token", body), (200, {"course": "hanoi", "version": "v1", "source": "print(1)"}))
                self.assertEqual(call("student-token", body, "GET")[0], 405)
                self.assertEqual(call("wrong-token", body)[0], 401)
                self.assertEqual(call("other-token", body)[0], 403)
                self.assertEqual(call("student-token", {**body, "source": ""})[0], 400)
                fake.fail = True
                self.assertEqual(call("student-token", body)[1]["error"]["code"], "ai.reply.invalid")
                server.course_debrief = None
                self.assertEqual(call("student-token", body)[0], 503)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
