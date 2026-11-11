from __future__ import annotations

import json
import unittest

from picopg.server.ai.course import OneSkill
from picopg.server.ai.model import CourseAiError
from picopg.server.ai.debrief import VERSION, CourseDebriefService, debrief_request

SKILL = {"course": "water-container", "rules": "同一个下标不能用两次。"}
REVIEW = {
    "idea": "每个 i 当矮边，找最远的不比它矮的 k", "right": "水量只由矮的那根决定", "cleaner": "```python\ndef max_area(height):\n    return 0\n```",
    "cleaner_why": "合成一段", "math": "max min(h_i,h_j)(j-i)", "better": "按高度从高到低", "better_code": None,
    "complexity": "O(n²) → O(n log n)", "edges": "等高也对",
    "lectures": [{"title": "从高到低", "steps": [
        {"title": "哪里在重复", "beats": [{"say": "每根线都重新扫一遍。"}]},
        {"title": "存起来", "beats": [{"say": "先排序。", "write": "```python\ndef max_area(height):\n    return 0\n```"}]},
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
        self.assertEqual(debrief["lectures"][0]["steps"][1]["beats"][0]["write"], "def max_area(height):\n    return 0\n")
        self.assertNotIn("write", debrief["lectures"][0]["steps"][0]["beats"][0])
        unfinished = {**REVIEW, "lectures": [{"title": "t", "steps": [{"title": "s", "beats": [{"say": "只讲不写。"}]}]}]}
        model = RecordingModel([json.dumps(unfinished, ensure_ascii=False), json.dumps(REVIEW, ensure_ascii=False)])
        CourseDebriefService(model, OneSkill(SKILL)).debrief("water-container", "v1", REQUEST)
        self.assertIn("最后一句要写出完整解法", model.calls[1][-1]["content"])

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


if __name__ == "__main__":
    unittest.main()
