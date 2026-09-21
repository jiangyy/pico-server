"""Tests for ``POST /api/v1/sync``.

``ServerTestCase`` shares one server and one database across a class, so
the content log keeps growing as siblings publish courses. Every
assertion here therefore reads the cursor the response reports instead of
assuming an absolute seq, and each test names its own Playground so the
classes cannot interfere.
"""

import unittest

from test_api import OPERATOR_TOKEN, ServerTestCase


class SyncEndpointTests(ServerTestCase):
    """The wire shape of ``POST /api/v1/sync`` and its per-kind behaviour."""

    def sync(self, pending_events, after_seq=0):
        status, _, body = self.request(
            "POST",
            "/api/v1/sync",
            payload={"pending_events": pending_events, "after_seq": after_seq},
        )
        self.assertEqual(status, 200, body)
        return body

    def submit_event(self, event_id, playground_id, version_id, text):
        return {
            "event_id": event_id,
            "kind": "submit_code",
            "payload": {
                "playground_id": playground_id,
                "version_id": version_id,
                "files": {"work/main.py": text},
            },
        }

    def test_the_response_carries_the_protocol_negotiation(self):
        body = self.sync([], after_seq=0)
        self.assertEqual(body["protocol_version"], 1)
        self.assertGreaterEqual(body["minimum_client_protocol_version"], 1)
        self.assertEqual(body["event_results"], [])
        self.assertIsInstance(body["upto_seq"], int)

    def test_an_accepted_submission_is_reported_and_stored(self):
        playground_id, commit_id = self.seed("pg-sync-submit")
        body = self.sync(
            [self.submit_event("e-sync-1", playground_id, commit_id, "# 姓名：张三\nprint(1)\n")]
        )
        self.assertEqual(
            body["event_results"],
            [{"event_id": "e-sync-1", "status": "accepted"}],
        )

        status, _, listing = self.request(
            "GET", "/api/v1/submissions", token=OPERATOR_TOKEN
        )
        self.assertEqual(status, 200)
        stored = [
            entry
            for entry in listing["submissions"]
            if entry["playgroundId"] == playground_id
        ]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["student"], "张三")

    def test_a_replayed_event_id_returns_the_stored_result(self):
        playground_id, commit_id = self.seed("pg-sync-replay")
        event = self.submit_event("e-sync-2", playground_id, commit_id, "# 姓名：李四\nprint(1)\n")
        first = self.sync([event])
        self.assertEqual(first["event_results"][0]["status"], "accepted")

        # The same id with tampered bytes must still replay: a retry after a
        # lost response cannot turn one hand-in into a second record.
        event["payload"]["files"]["work/main.py"] = "tampered"
        replay = self.sync([event])
        self.assertEqual(
            replay["event_results"], [{"event_id": "e-sync-2", "status": "accepted"}]
        )

    def test_a_submission_against_a_commit_that_is_not_published_is_rejected(self):
        playground_id, _ = self.seed("pg-sync-bad-version")
        body = self.sync(
            [
                self.submit_event(
                    "e-sync-3", playground_id, "f" * 40, "# 姓名：张三\nprint(1)\n"
                )
            ]
        )
        self.assertEqual(body["event_results"][0]["status"], "rejected")
        self.assertEqual(body["event_results"][0]["reason"], "invalid_version")

    def test_a_submission_without_a_header_is_rejected(self):
        playground_id, commit_id = self.seed("pg-sync-no-name")
        body = self.sync(
            [self.submit_event("e-sync-4", playground_id, commit_id, "print(1)\n")]
        )
        self.assertEqual(body["event_results"][0]["status"], "rejected")
        self.assertEqual(body["event_results"][0]["reason"], "invalid_submission")

    def test_a_path_outside_work_is_rejected(self):
        playground_id, commit_id = self.seed("pg-sync-bad-path")
        body = self.sync(
            [
                {
                    "event_id": "e-sync-5",
                    "kind": "submit_code",
                    "payload": {
                        "playground_id": playground_id,
                        "version_id": commit_id,
                        "files": {
                            "../../etc/passwd": "x",
                            "work/main.py": "# 姓名：张三\nprint(1)\n",
                        },
                    },
                }
            ]
        )
        self.assertEqual(body["event_results"][0]["status"], "rejected")
        self.assertEqual(body["event_results"][0]["reason"], "invalid_submission")

    def test_an_unknown_kind_is_rejected_without_failing_the_round(self):
        playground_id, commit_id = self.seed("pg-sync-unknown")
        body = self.sync(
            [
                {"event_id": "e-sync-unknown", "kind": "made_up", "payload": {}},
                self.submit_event(
                    "e-sync-6", playground_id, commit_id, "# 姓名：张三\nprint(1)\n"
                ),
            ]
        )
        self.assertEqual(
            body["event_results"][0],
            {
                "event_id": "e-sync-unknown",
                "status": "rejected",
                "reason": "unknown_kind",
            },
        )
        # The rest of the round still ran.
        self.assertEqual(body["event_results"][1]["status"], "accepted")

    def test_a_malformed_body_is_a_bad_request(self):
        for payload in (
            [],
            {"after_seq": 0},
            {"pending_events": [], "after_seq": -1},
            {"pending_events": [{"kind": "submit_code"}], "after_seq": 0},
            {"pending_events": [], "after_seq": "0"},
        ):
            with self.subTest(payload=payload):
                status, _, _ = self.request("POST", "/api/v1/sync", payload=payload)
                self.assertEqual(status, 400)

    def test_the_content_log_is_streamed_from_the_clients_cursor(self):
        playground_id, _ = self.seed("pg-sync-log")

        start = self.sync([], after_seq=0)
        kinds = [entry["event"]["kind"] for entry in start["entries"]]
        self.assertIn("publish_playground", kinds)
        # Every entry is nested exactly as protocol.md's ContentLogEntry.
        for entry in start["entries"]:
            self.assertIn("seq", entry)
            self.assertEqual(
                sorted(entry["event"]), ["event_id", "kind", "payload"]
            )

        # Reading again from the reported cursor must return nothing new.
        again = self.sync([], after_seq=start["upto_seq"])
        self.assertEqual(again["entries"], [])
        self.assertEqual(again["upto_seq"], start["upto_seq"])

    def test_archive_is_published_to_clients_through_the_log(self):
        playground_id, _ = self.seed("pg-sync-archive")
        before = self.sync([], after_seq=0)

        status, _, _ = self.request(
            "DELETE",
            f"/api/v1/playgrounds/{playground_id}",
            token=OPERATOR_TOKEN,
        )
        self.assertEqual(status, 200)

        after = self.sync([], after_seq=before["upto_seq"])
        archived = [
            entry
            for entry in after["entries"]
            if entry["event"]["kind"] == "archive_playground"
        ]
        self.assertEqual(len(archived), 1)
        self.assertEqual(
            archived[0]["event"]["payload"], {"playground_id": playground_id}
        )

    def test_a_submission_to_an_archived_playground_is_rejected(self):
        playground_id, commit_id = self.seed("pg-sync-archived-submit")
        self.request(
            "DELETE",
            f"/api/v1/playgrounds/{playground_id}",
            token=OPERATOR_TOKEN,
        )
        body = self.sync(
            [self.submit_event("e-sync-7", playground_id, commit_id, "# 姓名：张三\nprint(1)\n")]
        )
        self.assertEqual(body["event_results"][0]["status"], "rejected")
        self.assertEqual(body["event_results"][0]["reason"], "playground_stopped")

    def test_a_client_cannot_publish_or_archive_through_sync(self):
        playground_id, commit_id = self.seed("pg-sync-cannot-write")
        body = self.sync(
            [
                {
                    "event_id": "e-sync-publish",
                    "kind": "publish_playground",
                    "payload": {"playground_id": playground_id, "version_id": commit_id},
                },
                {
                    "event_id": "e-sync-archive",
                    "kind": "archive_playground",
                    "payload": {"playground_id": playground_id},
                },
            ]
        )
        self.assertEqual(
            [result["status"] for result in body["event_results"]],
            ["rejected", "rejected"],
        )
        self.assertEqual(
            [result["reason"] for result in body["event_results"]],
            ["invalid_event", "invalid_event"],
        )


if __name__ == "__main__":
    unittest.main()
