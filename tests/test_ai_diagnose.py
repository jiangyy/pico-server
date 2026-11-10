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

from picopg.server.ai.course import CourseSkillRepository
from picopg.server.ai.diagnose import VERSION, CourseDiagnoseService, diagnose_request
from picopg.server.ai.model import CourseAiError

SKILL = {"course": "hanoi", "actions": [], "concepts": ["recursion", "while"], "rules": "盘子只能顺时针搬到下一根柱子。"}
REQUEST = {"evidence": "## 现在的代码\n  8|         if src != dst:", "reference": "def hanoi(n, src, dst): ...", "deleted": ["while src != dst:"], "rejected": []}
GOOD = {"patch": [{"line": 8, "old": "if src != dst:", "text": "        while src != dst:"}], "concept": "while", "note": "把 while 当成了 if"}


class RecordingModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def step(self, messages, tools, on_text=None):
        self.calls.append([dict(message) for message in messages])
        return {"content": next(self.replies)}


class CourseDiagnoseServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        (root / "hanoi.json").write_text(json.dumps(SKILL, ensure_ascii=False), encoding="utf-8")
        self.skills = CourseSkillRepository(root)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_course_comes_first_and_the_student_after(self) -> None:
        model = RecordingModel([json.dumps(GOOD, ensure_ascii=False)])
        diagnosis = CourseDiagnoseService(model, self.skills).diagnose("hanoi", "v1", REQUEST)
        self.assertEqual(diagnosis, {**GOOD, "version": VERSION})
        system, user = model.calls[0]
        self.assertIn("recursion、while", system["content"])
        self.assertIn("def hanoi(n, src, dst)", system["content"])
        self.assertNotIn("if src != dst", system["content"])
        self.assertIn("`while src != dst:`", user["content"])

    def test_rejected_patches_are_sent_back_with_their_results(self) -> None:
        model = RecordingModel([json.dumps(GOOD, ensure_ascii=False)])
        rejected = [{"proposal": {"patch": [{"line": 9, "old": "x", "text": "y"}], "concept": "while", "note": ""}, "result": "结果 A -> B"}]
        CourseDiagnoseService(model, self.skills).diagnose("hanoi", "v1", {**REQUEST, "rejected": rejected})
        self.assertIn("结果 A -> B", model.calls[0][1]["content"])

    def test_a_concept_outside_the_course_list_is_sent_back_once(self) -> None:
        model = RecordingModel([json.dumps({**GOOD, "concept": "loops"}), json.dumps(GOOD, ensure_ascii=False)])
        diagnosis = CourseDiagnoseService(model, self.skills).diagnose("hanoi", "v1", REQUEST)
        self.assertEqual(diagnosis["concept"], "while")
        self.assertIn("concept 只能是", model.calls[1][-1]["content"])

    def test_still_invalid_after_one_retry_is_an_error(self) -> None:
        model = RecordingModel(['{"patch": []}', '{"patch": []}'])
        with self.assertRaises(CourseAiError):
            CourseDiagnoseService(model, self.skills).diagnose("hanoi", "v1", REQUEST)

    def test_request_shape_is_checked(self) -> None:
        body = {"playgroundId": "hanoi", "versionId": "v1", **REQUEST}
        self.assertEqual(diagnose_request(body)[2], REQUEST)
        with self.assertRaises(ValueError):
            diagnose_request({**body, "deleted": "x"})
        with self.assertRaises(ValueError):
            diagnose_request({**body, "extra": 1})


class FakeDiagnose:
    def diagnose(self, playground_id, version_id, request):
        return {**GOOD, "note": f"{playground_id}:{len(request['deleted'])}", "version": VERSION}


class DiagnoseRouteTest(unittest.TestCase):
    def test_diagnose_route(self) -> None:
        body = {"playgroundId": "hanoi", "versionId": "v1", **REQUEST}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = PrincipalSession.create("student", "device", "student-token", {}, ["playgrounds"])
            server = PicoPGHttpServer(
                ("127.0.0.1", 0), FileReplicaLogStore(root / "replica"), "operator-token", root / "packages",
                principal_sessions=[session], ai_access=AiAccess(["student"]), course_diagnose=FakeDiagnose(),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def post(token, value):
                connection = http.client.HTTPConnection(*server.server_address)
                try:
                    connection.request("POST", "/api/v1/ai/diagnose", json.dumps(value).encode(), {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()

            try:
                self.assertEqual(post("student-token", body), (200, {**GOOD, "note": "hanoi:1", "version": VERSION}))
                self.assertEqual(post("wrong-token", body)[0], 401)
                status, error = post("student-token", {**body, "rejected": "x"})
                self.assertEqual((status, error["error"]["code"]), (400, "ai.request.invalid"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
