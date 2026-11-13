from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from picopg.replica.file_store import FileReplicaLogStore
from picopg.server.ai.access import AiAccess
from picopg.server.ai.course import CourseSkillRepository
from picopg.server.ai.hint import CourseHintService, hint_request
from picopg.server.ai.model import CourseAiError, FallbackModel
from picopg.server.app import PicoPGHttpServer
from picopg.server.auth.model import PrincipalSession
from picopg.server.config import load_course_hint

SKILL = {
    "course": "hanoi",
    "actions": [],
    "rules": "盘子只能顺时针搬到下一根柱子。",
    "meanings": {"hanoi(n, src, dst)": "把 n 个盘子从 src 柱搬到 dst 柱"},
}
EVIDENCE = "## 为什么开口\n连续 3 次改了代码，结果都是没做对\n\n## 学生写的那一层：hanoi(n=3, src=\"A\", dst=\"C\")\n第 19 行 hanoi(n - 1, src, aux)：这一次是 hanoi(2, \"A\", \"B\")，产生第 1–5 步"
GOOD = {
    "diagnosis": "没懂 hanoi 的意思",
    "concept": "模型自己写的第一句",
    "reason": "第 20 行要把 3 号盘从 A 搬到 C，先到 B；B 上有第 19 行放的 1 号盘。",
    "header": "此刻执行 hanoi(3, \"A\", \"C\")：把 3 个盘从 A 搬到 C。",
    "glosses": [{"line": 19, "text": "把 2 个盘从 A 搬到 B，产生第 1–5 步"}, {"line": "x", "text": "坏的一条"}],
}


class RecordingModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def step(self, messages, tools, on_text=None):
        self.calls.append([dict(message) for message in messages])
        return {"content": next(self.replies)}


class CourseHintServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        (root / "hanoi.json").write_text(json.dumps(SKILL, ensure_ascii=False), encoding="utf-8")
        self.skills = CourseSkillRepository(root)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_first_sentence_comes_from_the_course_and_the_model_writes_the_rest(self) -> None:
        model = RecordingModel([json.dumps(GOOD, ensure_ascii=False)])
        hint = CourseHintService(model, self.skills).hint("hanoi", "v1", EVIDENCE, "hanoi")
        self.assertEqual(hint["concept"], "hanoi(n, src, dst) 就是一句话：把 n 个盘子从 src 柱搬到 dst 柱。")
        self.assertEqual(hint["reason"], GOOD["reason"])
        self.assertEqual(hint["glosses"], [GOOD["glosses"][0]], "malformed glosses are dropped")
        self.assertEqual(hint["problems"], [])
        system, user = model.calls[0]
        self.assertEqual(system["role"], "system")
        self.assertIn("第一句由课程给出", system["content"])
        for part in ("## 课程规则\n盘子只能顺时针", "hanoi(n, src, dst)：把 n 个盘子", EVIDENCE, "## 第一句（课程给出"):
            self.assertIn(part, user["content"])

    def test_code_to_teach_goes_in_the_example_not_the_reason(self) -> None:
        example = "nums = [5, 8, 2]\nfor i in range(len(nums)):  # i 依次是 0、1、2\n    print(nums[i])  # 依次打印 5、8、2"
        crammed = {**GOOD, "reason": "写成：\nfor i in range(len(height)):\n    pass"}
        model = RecordingModel([json.dumps(crammed, ensure_ascii=False), json.dumps({**GOOD, "example": example}, ensure_ascii=False)])
        hint = CourseHintService(model, self.skills).hint("hanoi", "v1", EVIDENCE, "hanoi")
        self.assertEqual((hint["example"], hint["problems"]), (example, []))
        self.assertIn("多行代码放进 example", model.calls[1][3]["content"])
        self.assertIsNone(CourseHintService(RecordingModel([json.dumps(GOOD, ensure_ascii=False)]), self.skills).hint("hanoi", "v1", EVIDENCE, "hanoi")["example"])

    def test_know_how_fixes_come_back_with_every_edit_and_malformed_ones_are_dropped(self) -> None:
        fixes = [
            {"why": "列表求和用 sum(copyh)", "review": "列表常用操作：len、sum、min、max、append", "edits": [{"line": 16, "old": "su=copyh.sum()", "new": "su=sum(copyh)"}, {"line": "x", "old": "a", "new": "b"}]},
            {"why": 3, "edits": []},
        ]
        model = RecordingModel([json.dumps({**GOOD, "fixes": fixes}, ensure_ascii=False)])
        hint = CourseHintService(model, self.skills).hint("hanoi", "v1", EVIDENCE, "hanoi")
        self.assertEqual(hint["fixes"], [{"why": fixes[0]["why"], "review": fixes[0]["review"], "edits": [fixes[0]["edits"][0]]}])
        self.assertIn("fixes", model.calls[0][0]["content"])
        self.assertEqual(CourseHintService(RecordingModel([json.dumps(GOOD, ensure_ascii=False)]), self.skills).hint("hanoi", "v1", EVIDENCE, "hanoi")["fixes"], [])

    def test_a_hint_that_breaks_the_rules_is_sent_back_once(self) -> None:
        long = {**GOOD, "reason": "看看第 20 行。" + "很" * 60}
        model = RecordingModel([json.dumps(long, ensure_ascii=False), json.dumps(GOOD, ensure_ascii=False)])
        hint = CourseHintService(model, self.skills).hint("hanoi", "v1", EVIDENCE, "hanoi")
        self.assertEqual((hint["reason"], hint["problems"]), (GOOD["reason"], []))
        revision = model.calls[1]
        self.assertEqual(revision[2]["role"], "assistant")
        self.assertIn("超过 60", revision[3]["content"])
        self.assertIn("叫学生去做什么", revision[3]["content"])

        stubborn = RecordingModel([json.dumps(long, ensure_ascii=False)] * 2)
        kept = CourseHintService(stubborn, self.skills).hint("hanoi", "v1", EVIDENCE, "hanoi")
        self.assertEqual(len(stubborn.calls), 2, "only one revision")
        self.assertTrue(kept["problems"], "a hint that is still wrong comes back with its problems")

    def test_without_course_meanings_the_model_writes_the_first_sentence_too(self) -> None:
        model = RecordingModel([json.dumps({**GOOD, "concept": "一" * 31}, ensure_ascii=False), json.dumps(GOOD, ensure_ascii=False)])
        hint = CourseHintService(model, self.skills).hint("sudoku", "v1", EVIDENCE, "solve")
        self.assertEqual(hint["concept"], GOOD["concept"])
        self.assertIn("concept 31 字", model.calls[1][3]["content"])
        self.assertNotIn("第一句由课程给出", model.calls[0][0]["content"])

    def test_a_reply_that_is_not_json_fails(self) -> None:
        with self.assertRaises(CourseAiError):
            CourseHintService(RecordingModel(["好的"]), self.skills).hint("hanoi", "v1", EVIDENCE, "hanoi")

    def test_requests_are_validated(self) -> None:
        body = {"playgroundId": "hanoi", "versionId": "v1", "evidence": EVIDENCE, "focus": "hanoi"}
        self.assertEqual(hint_request(body), ("hanoi", "v1", EVIDENCE, "hanoi"))
        self.assertEqual(hint_request({**body, "focus": None})[3], None)
        for bad in ({**body, "evidence": ""}, {**body, "focus": "a b"}, {**body, "playgroundId": "../x"}, {**body, "extra": 1}):
            with self.assertRaises(ValueError):
                hint_request(bad)

    def test_the_hint_model_can_differ_from_the_chat_model(self) -> None:
        settings = {
            "PICOPG_AI_BASE_URL": "https://one.example,https://two.example",
            "PICOPG_AI_MODEL": "chat-one,chat-two",
            "PICOPG_AI_HINT_MODEL": "reason-one,reason-two",
        }
        with mock.patch.dict(os.environ, settings, clear=False):
            service = load_course_hint()
        self.assertIsInstance(service.model, FallbackModel)
        self.assertEqual([model.model for model in service.model.models], ["reason-one", "reason-two"])
        with mock.patch.dict(os.environ, {**settings, "PICOPG_AI_HINT_MODEL": ""}, clear=False):
            self.assertEqual([model.model for model in load_course_hint().model.models], ["chat-one", "chat-two"])


class FakeHint:
    def hint(self, playground_id, version_id, evidence, focus):
        return {"concept": f"{playground_id}:{focus}", "reason": evidence[:4], "problems": []}


class HintRouteTest(unittest.TestCase):
    def serve(self, root: Path, hint):
        session = PrincipalSession.create("student", "device", "student-token", {}, ["playgrounds"])
        server = PicoPGHttpServer(
            ("127.0.0.1", 0), FileReplicaLogStore(root / "replica"), "operator-token", root / "packages",
            principal_sessions=[session], ai_access=AiAccess(["student"]), course_hint=hint,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def post(self, server, token, body):
        connection = http.client.HTTPConnection(*server.server_address)
        try:
            connection.request("POST", "/api/v1/ai/hint", json.dumps(body).encode(), {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_hint_route(self) -> None:
        body = {"playgroundId": "hanoi", "versionId": "v1", "evidence": EVIDENCE, "focus": "hanoi"}
        with tempfile.TemporaryDirectory() as temporary:
            server, thread = self.serve(Path(temporary), FakeHint())
            try:
                self.assertEqual(self.post(server, "student-token", body), (200, {"concept": "hanoi:hanoi", "reason": "## 为", "problems": []}))
                status, error = self.post(server, "wrong-token", body)
                self.assertEqual((status, error["error"]["code"]), (401, "auth.required"))
                status, error = self.post(server, "student-token", {**body, "focus": 3})
                self.assertEqual((status, error["error"]["code"]), (400, "ai.request.invalid"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
        with tempfile.TemporaryDirectory() as temporary:
            server, thread = self.serve(Path(temporary), None)
            try:
                status, error = self.post(server, "student-token", body)
                self.assertEqual((status, error["error"]["code"]), (503, "ai.unavailable"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
