"""Tests for immutable Submission records.

A submission is the one thing a client writes: a bounded file set recorded
against an exact published commit.  These tests drive the domain directly
against a real database and a real bare repository, so they check the rules a
request handler cannot: what is stored, what is refused, and what an exact
retry returns.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Allow ``import submissions`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bundles  # noqa: E402
import idempotency  # noqa: E402
import playgrounds  # noqa: E402
import repository  # noqa: E402
import submissions  # noqa: E402
from database import open_database  # noqa: E402

MANIFEST_BYTES = b'{"schemaVersion":1,"runtime":"python"}'
OPERATOR = "operator"


class SubmissionTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        base = Path(self.temporary_directory.name)
        self.connection = open_database(base / "app.db")
        self.addCleanup(self.connection.close)
        self.repository = repository.open_repository(base / "picopg.git")

        self.playground_id = "pg-one"
        playgrounds.create_playground(self.connection, self.playground_id)
        self.commit_id = playgrounds.publish_playground(
            self.connection,
            self.playground_id,
            self.repository,
            bundles.validate_bundle(
                [
                    bundles.BundleEntry("picopg.json", MANIFEST_BYTES),
                    bundles.BundleEntry("course/module/app.py", b"print(1)"),
                ]
            ),
            repository.CommitMetadata(
                name="PicoPG Operator",
                email="operator@picopg.invalid",
                timestamp="2026-01-01T00:00:00.000000Z",
                message="Publish playground bundle",
            ),
        ).commit_id

    def payload(self, **overrides):
        payload = {
            "playgroundId": self.playground_id,
            "commitId": self.commit_id,
            "student": "2026-001",
            "files": {"work/main.py": "print(1)\n"},
        }
        payload.update(overrides)
        return payload

    def create(self, payload=None, key="key-1"):
        request = submissions.decode_submission_request(payload or self.payload())
        return submissions.create_submission(
            self.connection, self.repository, request, key
        )


class DecodeSubmissionTest(SubmissionTestBase):
    """What a request body must look like before anything is stored."""

    def test_a_valid_body_decodes_into_a_normalized_request(self):
        request = submissions.decode_submission_request(self.payload())

        self.assertIsInstance(request, submissions.SubmissionRequest)
        self.assertEqual(request.playground_id, self.playground_id)
        self.assertEqual(request.playground_commit_id, self.commit_id)
        self.assertEqual(request.student, "2026-001")
        self.assertEqual(request.files, (("work/main.py", "print(1)\n"),))

    def test_the_student_name_is_trimmed_and_bounded(self):
        self.assertEqual(
            submissions.decode_submission_request(
                self.payload(student="  2026-001  ")
            ).student,
            "2026-001",
        )

        for student in ("", "   ", "a" * 129, "line\nbreak", None, 7):
            with self.subTest(student=student):
                with self.assertRaises(submissions.InvalidSubmissionRequestError):
                    submissions.decode_submission_request(self.payload(student=student))

    def test_the_commit_must_be_a_full_object_id(self):
        for commit_id in ("HEAD", "abc", "0" * 39, "A" * 40, None):
            with self.subTest(commit_id=commit_id):
                with self.assertRaises(submissions.InvalidSubmissionRequestError):
                    submissions.decode_submission_request(
                        self.payload(commitId=commit_id)
                    )

    def test_only_work_paths_may_be_handed_in(self):
        for path in ("course/index.html", "picopg.json", "work/../../x.py", "Work/a.py"):
            with self.subTest(path=path):
                with self.assertRaises(submissions.InvalidSubmissionRequestError):
                    submissions.decode_submission_request(
                        self.payload(files={path: "x"})
                    )

    def test_a_case_colliding_file_set_is_rejected(self):
        with self.assertRaises(submissions.InvalidSubmissionRequestError):
            submissions.decode_submission_request(
                self.payload(files={"work/A.py": "1", "work/a.py": "2"})
            )

    def test_files_must_be_text_and_bounded(self):
        with self.assertRaises(submissions.InvalidSubmissionRequestError):
            submissions.decode_submission_request(self.payload(files={"work/a.py": 7}))

        with self.assertRaises(submissions.InvalidSubmissionRequestError):
            submissions.decode_submission_request(self.payload(files={"work/a.py": "x" * (1024 * 1024 + 1)}))

    def test_shape_errors_are_rejected(self):
        for payload in (None, [], "text", {}, {"playgroundId": "pg-one"}):
            with self.subTest(payload=payload):
                with self.assertRaises(submissions.InvalidSubmissionRequestError):
                    submissions.decode_submission_request(payload)

    def test_a_file_set_without_files_is_allowed(self):
        request = submissions.decode_submission_request(self.payload(files={}))
        self.assertEqual(request.files, ())


class CreateSubmissionTest(SubmissionTestBase):
    """What actually gets recorded."""

    def test_a_submission_records_the_exact_commit_and_files(self):
        status, body = self.create()

        self.assertEqual(status, 201)
        mapping = body["submission"]
        self.assertEqual(mapping["playgroundId"], self.playground_id)
        self.assertEqual(mapping["commitId"], self.commit_id)
        self.assertEqual(mapping["student"], "2026-001")

        stored = submissions.get_submission(self.connection, mapping["id"])
        self.assertEqual(stored.files, (("work/main.py", "print(1)\n"),))

    def test_an_unpublished_commit_is_refused(self):
        request = submissions.decode_submission_request(
            self.payload(commitId="0" * 40)
        )

        with self.assertRaises(submissions.UnknownPlaygroundError):
            submissions.create_submission(
                self.connection, self.repository, request, "key-1"
            )

    def test_an_unknown_playground_is_refused(self):
        request = submissions.decode_submission_request(
            self.payload(playgroundId="pg-none")
        )

        with self.assertRaises(submissions.UnknownPlaygroundError):
            submissions.create_submission(
                self.connection, self.repository, request, "key-1"
            )

    def test_an_exact_retry_replays_the_stored_result(self):
        first = self.create()
        second = self.create()

        self.assertEqual(first, second)
        self.assertEqual(self.count_rows(), 1)

    def test_the_same_key_with_another_body_conflicts(self):
        self.create()

        with self.assertRaises(idempotency.IdempotencyConflict):
            self.create(self.payload(files={"work/main.py": "print(2)\n"}))

    def test_two_students_may_reuse_one_key(self):
        self.create(key="shared")
        self.create(self.payload(student="2026-002"), key="shared")

        self.assertEqual(self.count_rows(), 2)

    def test_a_submission_is_append_only(self):
        first = self.create()
        second = self.create(self.payload(files={"work/main.py": "print(2)\n"}), key="key-2")

        self.assertNotEqual(
            first[1]["submission"]["id"], second[1]["submission"]["id"]
        )
        stored = submissions.get_submission(
            self.connection, first[1]["submission"]["id"]
        )
        self.assertEqual(stored.files, (("work/main.py", "print(1)\n"),))

    def test_a_caller_must_supply_a_decoded_request(self):
        with self.assertRaises(submissions.InvalidSubmissionRequestError):
            submissions.create_submission(
                self.connection, self.repository, self.payload(), "key-1"
            )

    def count_rows(self):
        return self.connection.execute(
            "SELECT COUNT(*) AS n FROM submissions"
        ).fetchone()["n"]


class ReadSubmissionTest(SubmissionTestBase):
    """Reading back what was handed in."""

    def test_an_unknown_submission_is_missing(self):
        with self.assertRaises(submissions.MissingSubmissionError):
            submissions.get_submission(self.connection, "no-such-id")
        with self.assertRaises(submissions.MissingSubmissionError):
            submissions.get_submission(self.connection, None)

    def test_a_stored_row_that_cannot_be_represented_fails_closed(self):
        # The row passes every database constraint but names a commit that is
        # not an object id, so reading it back must fail closed instead of
        # handing a caller a value the API could never have produced.
        with self.connection:
            self.connection.execute(
                "INSERT INTO submissions (id, playground_id,"
                " playground_commit_id, student, files_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("sub-1", self.playground_id, "HEAD", "a", "{}", "2026-01-01T00:00:00Z"),
            )
        with self.assertRaises(submissions.StoredSubmissionError):
            submissions.get_submission(self.connection, "sub-1")

    def test_a_malformed_files_column_fails_closed(self):
        with self.connection:
            self.connection.execute(
                "INSERT INTO submissions (id, playground_id,"
                " playground_commit_id, student, files_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("sub-2", self.playground_id, self.commit_id, "a", "[]", "x"),
            )
        with self.assertRaises(submissions.StoredSubmissionError):
            submissions.get_submission(self.connection, "sub-2")

    def test_listing_is_newest_first_and_paged(self):
        for index in range(3):
            self.create(
                self.payload(student=f"2026-00{index}"), key=f"key-{index}"
            )

        page = submissions.list_submissions(self.connection, limit=2)
        self.assertEqual(len(page), 2)
        self.assertIsNotNone(page.next_key)
        self.assertEqual(
            [item.created_at for item in page.items],
            sorted((item.created_at for item in page.items), reverse=True),
        )

        rest = submissions.list_submissions(
            self.connection, limit=2, after=page.next_key
        )
        self.assertEqual(len(rest), 1)
        self.assertIsNone(rest.next_key)

    def test_a_non_positive_limit_is_refused(self):
        for limit in (0, -1, None, "2"):
            with self.subTest(limit=limit):
                with self.assertRaises(submissions.InvalidSubmissionRequestError):
                    submissions.list_submissions(self.connection, limit=limit)

    def test_the_mapping_carries_files_only_when_asked(self):
        stored = submissions.get_submission(
            self.connection, self.create()[1]["submission"]["id"]
        )

        self.assertNotIn(
            "files", submissions.submission_to_mapping(stored.submission)
        )
        self.assertEqual(
            submissions.submission_to_mapping(stored.submission, files=stored.files)[
                "files"
            ],
            [{"path": "work/main.py", "text": "print(1)\n"}],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
