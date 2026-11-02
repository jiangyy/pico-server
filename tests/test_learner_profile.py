import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from picopg.replica import ReplicatedRecord, record_id_of
from picopg.replica.file_store import FileReplicaLogStore
from picopg.server.analysis import evidence_log_id
from picopg.server.learner import LearnerProfiles, due, for_course, profile_of

KNOWLEDGE = {
    "version": 2,
    "concepts": [
        {"id": "variable", "label": "变量与赋值", "requires": [], "aliases": ["赋值"]},
        {"id": "for", "label": "for 循环", "requires": ["variable"], "aliases": ["range", "遍历"]},
        {"id": "break", "label": "提前结束与标记", "requires": ["for"], "aliases": ["break"]},
        {"id": "recursion", "label": "递归", "requires": [], "aliases": ["调用栈"]},
    ],
    "courses": {"sudoku": ["for", "break"], "hanoi": ["recursion"]},
    "migrations": {"1": {"loop": "for", "value": "variable"}},
}


def evidence(data, at=None, course="sudoku", session="s1"):
    record = {"type": "learning.evidence.recorded", "source": {"session": session, "sequence": 1},
              "subject": {"playgroundId": course, "versionId": "v1"},
              "payload": {"schema": "session.activity", "version": 1, "data": data}}
    if at is not None:
        record["recordedAt"] = at
    return record


def graded(verdict, concepts, at, task="t", **extra):
    return evidence({"kind": "ai.practice", "stage": "graded", "task": task, "verdict": verdict, "concepts": concepts, **extra}, at)


def answer(verdict, concepts, at, **extra):
    return evidence({"kind": "ai.answer", "question": "q", "answer": "a", "verdict": verdict, "concepts": concepts, **extra}, at)


def frozen(value):
    """日志里的数组以 tuple 计算记录 id。"""
    if isinstance(value, dict):
        return {key: frozen(item) for key, item in value.items()}
    return tuple(frozen(item) for item in value) if isinstance(value, list) else value


def rows(profile):
    return {row["concept"]: row for row in profile["concepts"]}


class ProfileRulesTest(unittest.TestCase):
    def test_status_follows_the_strong_evidence(self) -> None:
        profile = profile_of([
            evidence({"kind": "script.step", "script": "x", "step": 0, "concepts": ["variable"]}),
            graded("pass", ["for"], "2026-11-01T10:00:00Z"),
            answer("pass", ["for"], "2026-11-01T11:00:00Z"),
            graded("pass", ["break"], "2026-11-01T10:00:00Z"),
            graded("fail", ["break"], "2026-11-01T12:00:00Z"),
        ], KNOWLEDGE)
        found = rows(profile)
        self.assertEqual(found["variable"]["status"], "learning", "only seen in a lesson")
        self.assertEqual(found["for"]["status"], "solid", "the last two judgments both passed")
        self.assertEqual(found["break"]["status"], "shaky", "the latest judgment failed")
        self.assertEqual([row["concept"] for row in profile["concepts"]], ["break", "variable", "for"], "weakest first")
        self.assertEqual(found["for"]["reviewDue"], "2026-11-02T11:00:00+00:00", "first review a day after the last pass")
        self.assertEqual(due(profile, datetime(2026, 11, 3, tzinfo=timezone.utc)), ["for"])
        self.assertEqual(due(profile, datetime(2026, 11, 2, tzinfo=timezone.utc)), [])

    def test_predictions_alone_never_make_a_concept_solid(self) -> None:
        right = evidence({"kind": "script.prediction", "script": "x", "step": 1, "choice": 0, "correct": True, "concepts": ["for"]})
        wrong = evidence({"kind": "script.prediction", "script": "x", "step": 1, "choice": 1, "correct": False, "concepts": ["break"]})
        found = rows(profile_of([right, right, right, wrong, wrong], KNOWLEDGE))
        self.assertEqual(found["for"]["status"], "learning")
        self.assertEqual(found["for"]["predictions"], {"correct": 3, "total": 3})
        self.assertEqual(found["break"]["status"], "shaky", "two wrong predictions out of two")

    def test_a_repeated_misconception_marks_the_concept_shaky(self) -> None:
        profile = profile_of([
            answer("partial", ["break"], "2026-11-01T10:00:00Z", misconception="break-inner-only"),
            graded("pass", ["break"], "2026-11-01T11:00:00Z"),
            answer("fail", ["break"], "2026-11-01T12:00:00Z", misconception="break-inner-only"),
            answer("pass", ["break"], "2026-11-01T13:00:00Z"),
        ], KNOWLEDGE)
        self.assertEqual(rows(profile)["break"]["status"], "shaky", "passing again does not hide a misconception seen twice")
        self.assertEqual(profile["misconceptions"], [
            {"id": "break-inner-only", "concepts": ["break"], "count": 2, "course": "sudoku", "last": "2026-11-01T12:00:00Z"},
        ])

    def test_old_concept_ids_migrate_and_unknown_ones_drop(self) -> None:
        found = rows(profile_of([graded("pass", ["loop", "motion"], "2026-11-01T10:00:00Z")], KNOWLEDGE))
        self.assertEqual(list(found), ["for"])

    def test_questions_take_their_concept_from_the_lesson_step_or_their_words(self) -> None:
        profile = profile_of([
            evidence({"kind": "script.step", "script": "hanoi", "step": 3, "concepts": ["recursion"]}, course="hanoi"),
            evidence({"kind": "ai.question", "text": "这里为什么又调用一次？", "script": {"id": "hanoi", "step": 3}}, course="hanoi"),
            evidence({"kind": "ai.question", "text": "range 停在哪？", "script": None}),
            evidence({"kind": "ai.question", "text": "for i in x", "explain": True, "lines": [1, 1], "script": None}),
        ], KNOWLEDGE)
        found = rows(profile)
        self.assertEqual(found["recursion"]["questions"], 1)
        self.assertEqual(found["for"]["questions"], 1, "range is an alias of for; the explain request names no concept")
        self.assertEqual((profile["habits"]["questions"], profile["habits"]["explains"]), (2, 1))

    def test_habits_and_open_practice(self) -> None:
        profile = profile_of([
            evidence({"kind": "ai.practice", "stage": "assigned", "task": "打印 1 到 5", "concepts": ["for"]}),
            graded("fail", ["for"], "2026-11-01T10:00:00Z", task="打印 1 到 5", attempt=1, asked=2),
            graded("pass", ["for"], "2026-11-01T10:05:00Z", task="打印 1 到 5", attempt=2, asked=0),
            evidence({"kind": "ai.practice", "stage": "assigned", "task": "倒着打印", "concepts": ["for"]}),
            evidence({"kind": "run.finished", "status": "error", "error": {"type": "NameError", "line": 2, "message": "x"}}),
            answer("partial", ["for"], "2026-11-01T11:00:00Z"),
        ], KNOWLEDGE)
        habits = profile["habits"]
        self.assertEqual((habits["passedPractice"], habits["attemptsToPass"], habits["askedBeforeSubmit"]), (1, 2.0, 1.0))
        self.assertEqual(habits["errors"], {"NameError": 1})
        self.assertEqual(habits["answers"], {"partial": 1})
        self.assertEqual(profile["open"], [{"task": "倒着打印", "course": "sudoku", "concepts": ["for"]}])

    def test_the_course_slice_keeps_its_concepts_and_weak_prerequisites(self) -> None:
        profile = profile_of([
            answer("fail", ["variable"], "2026-11-01T10:00:00Z", misconception="assignment-is-equation"),
            graded("pass", ["for"], "2026-11-01T10:00:00Z"),
            graded("pass", ["recursion"], "2026-11-01T10:00:00Z", course="hanoi"),
        ], KNOWLEDGE)
        piece = for_course(profile, "sudoku", KNOWLEDGE, datetime(2026, 11, 2, tzinfo=timezone.utc))
        self.assertEqual(sorted(row["concept"] for row in piece["concepts"]), ["for", "variable"],
                         "the shaky prerequisite of for comes along; another course's concept does not")
        self.assertEqual([entry["id"] for entry in piece["misconceptions"]], ["assignment-is-equation"])


class LearnerProfilesTest(unittest.TestCase):
    def test_profiles_read_every_device_and_cache_until_new_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = FileReplicaLogStore(Path(temporary))
            heads = {}

            def append(device, data, principal="student"):
                log = evidence_log_id(principal, device)
                data = frozen(data)
                record = ReplicatedRecord(record_id_of(log, heads.get(log), (), data), log, heads.get(log), (), data)
                self.assertEqual(store.append(log, heads.get(log), (record,)).kind, "accepted")
                heads[log] = record.position

            append("laptop", graded("pass", ["for"], "2026-11-01T10:00:00Z"))
            append("phone", graded("fail", ["for"], "2026-11-01T11:00:00Z"))
            append("laptop", graded("pass", ["break"], "2026-11-01T09:00:00Z"), principal="other")
            profiles = LearnerProfiles(store, lambda: KNOWLEDGE)
            first = profiles.profile("student")
            self.assertEqual(rows(first)["for"]["practice"], {"pass": 1, "fail": 1}, "both devices count")
            self.assertEqual(rows(first)["for"]["status"], "shaky", "merged by time: the phone's failure is the latest")
            self.assertNotIn("break", rows(first), "another student's evidence stays out")
            self.assertIs(profiles.profile("student"), first, "nothing new: the cached profile is reused")
            append("laptop", graded("pass", ["for"], "2026-11-01T12:00:00Z"))
            self.assertEqual(rows(profiles.profile("student"))["for"]["status"], "learning", "one pass after a failure is not mastery yet")
            self.assertEqual(profiles.profile("nobody")["concepts"], [])


if __name__ == "__main__":
    unittest.main()
