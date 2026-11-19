


"""Class roster authority and teacher Submission access."""

from __future__ import annotations

import unittest
import json
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


from picopg.server.app import PicoPGHttpServer
from picopg.server.auth.model import PrincipalSession
from picopg.server.drafts.protocol import revision_log_id
from picopg.replica import ReplicatedRecord, record_id_of
from picopg.replica.file_store import FileReplicaLogStore


def _record(log_id, parent, requires, data):
    return ReplicatedRecord(
        record_id_of(log_id, parent, requires, data), log_id, parent, requires, data
    )


def _submission(store, principal_id: str, replica_id: str) -> ReplicatedRecord:
    revision_log = revision_log_id(principal_id, "lesson", replica_id)
    revision = _record(revision_log, None, (), {
        "type": "draft.revised",
        "draftId": "lesson",
        "replicaId": replica_id,
        "source": f"print({principal_id!r})\n",
        "base": None,
    })
    assert store.append(revision_log, None, (revision,)).kind == "accepted"
    request_log = f"principal/{principal_id}/submission/{replica_id}/requests"
    request = _record(request_log, None, (revision.position,), {
        "type": "submission.requested",
        "submissionId": f"submission-{principal_id}",
        "playgroundId": "lesson",
        "revision": revision.position.to_value(),
    })
    assert store.append(request_log, None, (request,)).kind == "accepted"
    return request


def _request(
    base_url: str,
    path: str,
    token: str,
    *,
    method: str = "GET",
    value: object | None = None,
) -> tuple[int, object]:
    body = None if value is None else json.dumps(value).encode()
    request = urllib.request.Request(
        base_url + path,
        method=method,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read())


def _timed_submission(store, principal_id: str, replica_id: str, submitted_at: str) -> ReplicatedRecord:
    revision_log = revision_log_id(principal_id, "lesson", replica_id)
    parent = store.head(revision_log)
    revision = _record(revision_log, parent, (), {
        "type": "draft.revised", "draftId": "lesson", "replicaId": replica_id,
        "source": f"print({submitted_at!r})\n", "base": parent.to_value() if parent else None,
    })
    assert store.append(revision_log, parent, (revision,)).kind == "accepted"
    request_log = f"principal/{principal_id}/submission/{replica_id}/requests"
    head = store.head(request_log)
    request = _record(request_log, head, (revision.position,), {
        "type": "submission.requested", "submissionId": f"{principal_id}-{submitted_at}", "playgroundId": "lesson",
        "revision": revision.position.to_value(), "submittedAt": submitted_at,
    })
    assert store.append(request_log, head, (request,)).kind == "accepted"
    return request


class ClassMembershipTest(unittest.TestCase):
    def test_a_comment_on_code_keeps_its_kind_anchor_and_suggested_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileReplicaLogStore(root / "replica")
            request = _submission(store, "s1", "device")
            sessions = tuple(PrincipalSession.create(principal, "device", f"{principal}-token", {}, ["submissions"] if principal == "s1" else ()) for principal in ("t1", "s1"))
            server = PicoPGHttpServer(("127.0.0.1", 0), store, "operator-token", root / "packages", principal_sessions=sessions)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            path = f"/api/v1/teacher/submissions/{quote(request.id, safe='')}"
            anchor = {"from": {"line": 1, "column": 7}, "to": {"line": 1, "column": 11}, "quote": "'s1'"}
            suggest = {"text": "用变量名", "kind": "suggest", "anchor": anchor, "replacement": "name"}
            try:
                assert _request(base_url, "/api/v1/operator/classes/c", "operator-token", method="POST",
                                value={"teachers": ["t1"], "students": ["s1"]})[0] == 201
                assert _request(base_url, f"{path}/comments", "t1-token", method="POST", value=suggest)[0] == 201
                assert _request(base_url, f"{path}/comments", "t1-token", method="POST", value={"text": "整体不错"})[0] == 201
                for wrong in (
                    {"text": "x", "kind": "scribble", "anchor": anchor},
                    {"text": "x", "kind": "note"},
                    {"text": "x", "kind": "note", "anchor": anchor, "replacement": "y"},
                    {"text": "x", "kind": "note", "anchor": {**anchor, "to": {"line": 0, "column": 1}}},
                    {"text": "x", "kind": "note", "anchor": {**anchor, "quote": ""}},
                    {"text": "x", "color": "red"},
                ):
                    status, reply = _request(base_url, f"{path}/comments", "t1-token", method="POST", value=wrong)
                    assert status == 400, (wrong, status, reply)

                status, detail = _request(base_url, path, "t1-token")
                assert status == 200
                shown = [{key: value for key, value in item.items() if key not in ("position", "createdAt")} for item in detail["comments"]]
                assert shown == [{"author": "t1", **suggest}, {"author": "t1", "text": "整体不错"}]
                status, history = _request(base_url, "/api/v1/submissions", "s1-token")
                assert status == 200
                [mine] = history["submissions"]
                assert [{key: value for key, value in item.items() if key != "createdAt"} for item in mine["comments"]] == shown
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_one_broken_request_does_not_hide_the_class_and_submissions_are_in_time_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileReplicaLogStore(root / "replica")
            # 同一个学生先用 phone 交、再用 laptop 交：日志 id 的顺序和提交时间相反。
            _timed_submission(store, "s1", "phone", "2026-11-19T08:00:00Z")
            _timed_submission(store, "s1", "laptop", "2026-11-19T09:00:00Z")
            broken_log = "principal/s2/submission/device/requests"
            store.append(broken_log, None, (_record(broken_log, None, (), {"type": "submission.requested", "submissionId": "x"}),))
            # 16:30+08:00 就是 UTC 08:30：按时间排在中间，按字符串会排到最后。
            _timed_submission(store, "s2", "other", "2026-11-19T16:30:00+08:00")
            sessions = tuple(PrincipalSession.create(principal, "device", f"{principal}-token", {}, ()) for principal in ("t1", "s1", "s2"))
            server = PicoPGHttpServer(("127.0.0.1", 0), store, "operator-token", root / "packages", principal_sessions=sessions)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            try:
                assert _request(base_url, "/api/v1/operator/classes/c", "operator-token", method="POST",
                                value={"teachers": ["t1"], "students": ["s1", "s2"]})[0] == 201
                status, listed = _request(base_url, "/api/v1/teacher/submissions", "t1-token")
                assert status == 200, listed
                assert [(item["principalId"], item["submittedAt"]) for item in listed["submissions"]] == [
                    ("s1", "2026-11-19T08:00:00Z"), ("s2", "2026-11-19T16:30:00+08:00"), ("s1", "2026-11-19T09:00:00Z"),
                ]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_class_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileReplicaLogStore(root / "replica")
            s1 = _submission(store, "s1", "s1-device")
            _submission(store, "s2", "s2-device")
            s3 = _submission(store, "s3", "s3-device")
            sessions = tuple(
                PrincipalSession.create(principal, "device", f"{principal}-token", {}, ())
                for principal in ("t1", "t2", "s1", "s2", "s3")
            )
            server = PicoPGHttpServer(
                ("127.0.0.1", 0),
                store,
                "operator-token",
                root / "packages",
                principal_sessions=sessions,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            try:
                status, _ = _request(
                    base_url,
                    "/api/v1/operator/classes/python-1",
                    "operator-token",
                    method="POST",
                    value={"teachers": ["t1"], "students": ["s2", "s1"]},
                )
                assert status == 201
                directory = server.class_rosters.directory()
                assert directory.students_of("t1") == frozenset(("s1", "s2"))
                assert [(roster.class_id, roster.students) for roster in directory.classes_of("t1")] == [("python-1", frozenset(("s1", "s2")))]
                assert directory.classes_of("s1") == ()

                status, listed = _request(
                    base_url, "/api/v1/teacher/submissions", "t1-token"
                )
                assert status == 200
                assert {item["principalId"] for item in listed["submissions"]} == {"s1", "s2"}
                assert _request(
                    base_url, "/api/v1/teacher/submissions", "t2-token"
                ) == (200, {"submissions": []})

                s1_id = quote(s1.id, safe="")
                s3_id = quote(s3.id, safe="")
                status, detail = _request(
                    base_url, f"/api/v1/teacher/submissions/{s1_id}", "t1-token"
                )
                assert status == 200
                assert detail["source"] == "print('s1')\n"
                assert _request(
                    base_url, f"/api/v1/teacher/submissions/{s3_id}", "t1-token"
                )[0] == 404

                status, _ = _request(
                    base_url,
                    f"/api/v1/teacher/submissions/{s1_id}/comments",
                    "t1-token",
                    method="POST",
                    value={"text": "Good."},
                )
                assert status == 201
                assert _request(
                    base_url,
                    f"/api/v1/teacher/submissions/{s1_id}/comments",
                    "t1-token",
                    method="POST",
                    value={"author": "forged", "text": "No."},
                )[0] == 400
                _, detail = _request(
                    base_url, f"/api/v1/teacher/submissions/{s1_id}", "t1-token"
                )
                assert detail["comments"][0]["author"] == "t1"

                status, _ = _request(
                    base_url,
                    "/api/v1/operator/classes/python-1",
                    "operator-token",
                    method="POST",
                    value={"teachers": ["t1"], "students": ["s3"]},
                )
                assert status == 201
                assert _request(
                    base_url, f"/api/v1/teacher/submissions/{s1_id}", "t1-token"
                )[0] == 404
                assert _request(
                    base_url, f"/api/v1/teacher/submissions/{s3_id}", "t1-token"
                )[0] == 200

                assert _request(
                    base_url,
                    "/api/v1/operator/classes/python-2",
                    "operator-token",
                    method="POST",
                    value={"teachers": ["unknown"], "students": []},
                )[0] == 400
                assert _request(
                    base_url,
                    "/api/v1/operator/classes/Bad",
                    "operator-token",
                    method="POST",
                    value={"teachers": ["t1"], "students": []},
                )[0] == 400
                assert _request(
                    base_url,
                    "/api/v1/operator/classes/python-2",
                    "wrong",
                    method="POST",
                    value={"teachers": ["t1"], "students": []},
                )[0] == 401
                roster_log = quote("class/python-1/roster", safe="")
                assert _request(
                    base_url,
                    f"/api/v1/replica/logs/{roster_log}/records",
                    "t1-token",
                )[0] == 403
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()


class DraftingAgent:
    def __init__(self) -> None:
        self.drafts = []

    def draft_comment(self, playground_id, version_id, source, learner=None):
        self.drafts.append((playground_id, source, learner))
        return "**做对了**：打印了名字。"


def _evidence(store, principal_id: str, device: str, data_items) -> None:
    from picopg.server.analysis import evidence_log_id

    log = evidence_log_id(principal_id, device)
    parent = None
    for index, data in enumerate(data_items):
        record = _record(log, parent, (), {
            "type": "learning.evidence.recorded", "recordedAt": f"2026-11-0{index + 1}T10:00:00Z",
            "source": {"session": "s", "sequence": index + 1},
            "subject": {"playgroundId": "lesson", "versionId": "v1"},
            "payload": {"schema": "session.activity", "version": 1, "data": data},
        })
        assert store.append(log, parent, (record,)).kind == "accepted"
        parent = record.position


class TeacherInsightTest(unittest.TestCase):
    def test_teachers_see_their_classes_activity_and_ai_drafts(self) -> None:
        from picopg.server.ai.access import AiAccess

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = FileReplicaLogStore(root / "replica")
            s1 = _submission(store, "s1", "s1-device")
            _evidence(store, "s1", "s1-device", [
                {"kind": "ai.question", "text": "for 循环怎么停？", "script": None},
                {"kind": "ai.answer", "question": "range(3) 几个数？", "answer": "4 个", "verdict": "fail",
                 "wrong": "range 不含终点", "concepts": ("for",)},
                {"kind": "run.finished", "status": "ok", "returncode": 0, "error": None},
            ])
            sessions = tuple(
                PrincipalSession.create(principal, "device", f"{principal}-token", {}, ())
                for principal in ("t1", "t2", "s1", "s2")
            )
            agent = DraftingAgent()
            server = PicoPGHttpServer(
                ("127.0.0.1", 0), store, "operator-token", root / "packages",
                principal_sessions=sessions, course_agent=agent, ai_access=AiAccess(["t1"]),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            try:
                for class_id, students in (("python-1", ["s1"]), ("python-2", ["s2"])):
                    assert _request(base_url, f"/api/v1/operator/classes/{class_id}", "operator-token",
                                    method="POST", value={"teachers": ["t1"], "students": students})[0] == 201

                status, value = _request(base_url, "/api/v1/teacher/classes", "t1-token")
                assert status == 200
                assert [(item["classId"], [student["principalId"] for student in item["students"]]) for item in value["classes"]] \
                    == [("python-1", ["s1"]), ("python-2", ["s2"])]
                profile = value["classes"][0]["students"][0]["profile"]
                assert profile["concepts"][0]["concept"] == "for" and profile["concepts"][0]["status"] == "shaky"
                assert profile["lastActive"] == "2026-11-03T10:00:00Z"
                assert _request(base_url, "/api/v1/teacher/classes", "t2-token") == (200, {"classes": []})

                status, value = _request(base_url, "/api/v1/teacher/students/s1/activity", "t1-token")
                assert status == 200
                assert [item["kind"] for item in value["activity"]] == ["ai.answer", "ai.question"], "newest first, runs left out"
                assert value["activity"][0]["wrong"] == "range 不含终点"
                assert _request(base_url, "/api/v1/teacher/students/s1/activity", "t2-token")[0] == 404, "not t2's student"

                s1_id = quote(s1.id, safe="")
                status, value = _request(base_url, f"/api/v1/teacher/submissions/{s1_id}/draft", "t1-token", method="POST", value={})
                assert (status, value) == (200, {"text": "**做对了**：打印了名字。"})
                playground, source, learner = agent.drafts[0]
                assert (playground, source) == ("lesson", "print('s1')\n")
                assert set(learner) == {"concepts", "misconceptions", "habits", "due", "open"}, "the draft sees the student's profile"
                assert _request(base_url, f"/api/v1/teacher/submissions/{s1_id}/draft", "t2-token", method="POST", value={})[0] == 404, \
                    "only for the teacher's own students"
                assert _request(base_url, "/api/v1/operator/classes/python-3", "operator-token",
                                method="POST", value={"teachers": ["t2"], "students": ["s1"]})[0] == 201
                assert _request(base_url, f"/api/v1/teacher/submissions/{s1_id}/draft", "t2-token", method="POST", value={})[0] == 403, \
                    "AI stays off for teachers the operator did not enable"
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
