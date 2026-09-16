"""Tests for immutable Comment creation, reads, and internal sources.

The tests reuse the real Playground/Work/Class/Release/Submission fixtures from
``test_submissions`` and exercise the Teacher ownership rule, owner changes,
Student/Admin access, body limits, idempotent retries, the disabled internal
AI/system path, ordering, and the absence of any mutation API.
"""

import dataclasses
import logging
import os
import sys
import unittest
import uuid
from datetime import datetime, timezone

# Allow ``import comments`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import accounts  # noqa: E402
import classes  # noqa: E402
import comments  # noqa: E402
import idempotency  # noqa: E402
import submissions  # noqa: E402
import test_submissions  # noqa: E402


_UNSET = object()


class CommentTestBase(test_submissions.SubmissionTestBase):
    def setUp(self):
        super().setUp()
        self.submission = self.submit()[1]["submission"]

        # A second Teacher/Class/Student/Release/Submission lane that the first
        # Teacher and Student must never reach.
        self.other_teacher = self.create_user("teacher")
        self.other_class = classes.create_class(
            self.connection, self.admin, "Elsewhere", self.other_teacher.id
        )
        self.assign_student(self.other_student, self.other_class.id)
        self.other_release = self.release_for(
            teacher=self.other_teacher, class_id=self.other_class.id
        )
        self.other_work = self.create_work(actor=self.other_student)
        self.other_submission = self.submit(
            actor=self.other_student,
            release_id=self.other_release["id"],
            work_commit_id=self.other_work,
        )[1]["submission"]

    # ----- helpers -------------------------------------------------------

    def comment(
        self,
        actor=_UNSET,
        submission_id=_UNSET,
        body="Great work",
        key=None,
        now=_UNSET,
    ):
        actor = self.teacher if actor is _UNSET else actor
        submission_id = (
            self.submission["id"] if submission_id is _UNSET else submission_id
        )
        now = None if now is _UNSET else now
        return comments.create_comment(
            self.connection,
            actor,
            submission_id,
            body,
            key or uuid.uuid4().hex,
            now=now,
        )

    def internal_comment(
        self,
        capability,
        service_user_id=_UNSET,
        submission_id=_UNSET,
        source="ai",
        body="AI note",
        key=None,
        now=_UNSET,
    ):
        service_user_id = (
            self.admin.id if service_user_id is _UNSET else service_user_id
        )
        submission_id = (
            self.submission["id"] if submission_id is _UNSET else submission_id
        )
        now = None if now is _UNSET else now
        return comments.create_internal_comment(
            self.connection,
            capability,
            service_user_id,
            submission_id,
            source,
            body,
            key or uuid.uuid4().hex,
            now=now,
        )

    def comment_rows(self, submission_id=_UNSET):
        if submission_id is _UNSET:
            return self.connection.execute(
                "SELECT * FROM comments ORDER BY created_at, id"
            ).fetchall()
        return self.connection.execute(
            """
            SELECT * FROM comments
            WHERE submission_id = ?
            ORDER BY created_at, id
            """,
            (submission_id,),
        ).fetchall()


class CommentCreationTest(CommentTestBase):
    def test_owning_teacher_creates_a_teacher_comment(self):
        status, body = self.comment(body="Nice work")

        self.assertEqual(status, 201)
        record = body["comment"]
        self.assertEqual(record["submissionId"], self.submission["id"])
        self.assertEqual(record["authorUserId"], self.teacher.id)
        self.assertEqual(record["source"], "teacher")
        self.assertEqual(record["body"], "Nice work")
        self.assertTrue(record["createdAt"])

        rows = self.comment_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["submission_id"], self.submission["id"])
        self.assertEqual(rows[0]["author_user_id"], self.teacher.id)
        self.assertEqual(rows[0]["source"], "teacher")
        self.assertEqual(rows[0]["body"], "Nice work")

    def test_unrelated_teacher_cannot_create(self):
        with self.assertRaises(accounts.ForbiddenActionError):
            self.comment(actor=self.other_teacher)
        self.assertEqual(self.comment_rows(), [])

    def test_owner_change_revokes_and_grants_creation(self):
        new_teacher = self.create_user("teacher")
        self.assertEqual(self.comment()[0], 201)

        classes.change_class_owner(
            self.connection, self.admin, self.klass.id, new_teacher.id
        )
        with self.assertRaises(accounts.ForbiddenActionError):
            self.comment(actor=self.teacher, key=uuid.uuid4().hex)

        status, body = self.comment(actor=new_teacher)
        self.assertEqual(status, 201)
        self.assertEqual(body["comment"]["authorUserId"], new_teacher.id)

        classes.change_class_owner(
            self.connection, self.admin, self.klass.id, self.teacher.id
        )
        self.assertEqual(self.comment()[0], 201)
        self.assertEqual(len(self.comment_rows()), 3)

    def test_non_teacher_actors_cannot_create(self):
        for actor in (
            self.student,
            self.other_student,
            self.admin,
            None,
            "user",
            7,
        ):
            with self.subTest(actor=actor):
                with self.assertRaises(accounts.ForbiddenActionError):
                    self.comment(actor=actor)
        self.assertEqual(self.comment_rows(), [])

    def test_unknown_submission_is_rejected(self):
        with self.assertRaises(submissions.MissingSubmissionError):
            self.comment(submission_id=uuid.uuid4().hex)
        self.assertEqual(self.comment_rows(), [])

    def test_malformed_submission_id_is_rejected(self):
        for submission_id in ("", None, 7):
            with self.subTest(submission_id=submission_id):
                with self.assertRaises(comments.InvalidCommentRequestError):
                    self.comment(submission_id=submission_id)
        self.assertEqual(self.comment_rows(), [])

    def test_fake_source_field_is_not_accepted(self):
        # The create API has no source parameter, so a bearer caller cannot
        # select AI/system feedback.
        with self.assertRaises(TypeError):
            comments.create_comment(
                self.connection,
                self.teacher,
                self.submission["id"],
                "hello",
                uuid.uuid4().hex,
                source="ai",
            )

        _, body = self.comment()
        self.assertEqual(body["comment"]["source"], "teacher")
        self.assertEqual(self.comment_rows()[0]["source"], "teacher")

    def test_body_validation(self):
        for body in ("", None, 7, b"bytes", "\ud800"):
            with self.subTest(body=body):
                with self.assertRaises(comments.InvalidCommentRequestError):
                    self.comment(body=body)
        self.assertEqual(self.comment_rows(), [])

        exact = "x" * comments.MAX_COMMENT_BODY_BYTES
        status, body = self.comment(body=exact)
        self.assertEqual(status, 201)
        self.assertEqual(body["comment"]["body"], exact)

        # One extra byte at the UTF-8 boundary is rejected.
        over = "x" * (comments.MAX_COMMENT_BODY_BYTES - 1) + "\u00e9"
        with self.assertRaises(comments.InvalidCommentRequestError):
            self.comment(body=over, key=uuid.uuid4().hex)
        self.assertEqual(len(self.comment_rows()), 1)

    def test_body_is_stored_verbatim_without_markup_interpretation(self):
        raw = "<b>bold</b> & <script>alert(1)</script>\n*md* `code`"
        status, body = self.comment(body=raw)
        self.assertEqual(status, 201)
        self.assertEqual(body["comment"]["body"], raw)
        self.assertEqual(self.comment_rows()[0]["body"], raw)

    def test_body_is_not_logged(self):
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record)
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            status, _ = self.comment(body="secret-student-excerpt")
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)

        self.assertEqual(status, 201)
        self.assertFalse(
            any(
                "secret-student-excerpt" in record.getMessage()
                for record in records
            )
        )


class CommentReadTest(CommentTestBase):
    def test_student_lists_only_own_submission(self):
        self.comment()

        listed = comments.list_comments(
            self.connection, self.student, self.submission["id"]
        )
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].submission_id, self.submission["id"])

        with self.assertRaises(submissions.MissingSubmissionError):
            comments.list_comments(
                self.connection, self.other_student, self.submission["id"]
            )

        own = comments.list_comments(
            self.connection, self.other_student, self.other_submission["id"]
        )
        self.assertEqual(own, [])

    def test_teacher_lists_only_owned_class_submissions(self):
        self.comment()

        listed = comments.list_comments(
            self.connection, self.teacher, self.submission["id"]
        )
        self.assertEqual(len(listed), 1)

        with self.assertRaises(submissions.MissingSubmissionError):
            comments.list_comments(
                self.connection, self.other_teacher, self.submission["id"]
            )

        own = comments.list_comments(
            self.connection, self.other_teacher, self.other_submission["id"]
        )
        self.assertEqual(own, [])

    def test_teacher_read_access_follows_owner_change(self):
        self.comment()
        new_teacher = self.create_user("teacher")

        classes.change_class_owner(
            self.connection, self.admin, self.klass.id, new_teacher.id
        )
        with self.assertRaises(submissions.MissingSubmissionError):
            comments.list_comments(
                self.connection, self.teacher, self.submission["id"]
            )
        self.assertEqual(
            len(
                comments.list_comments(
                    self.connection, new_teacher, self.submission["id"]
                )
            ),
            1,
        )

    def test_admin_is_denied_body_access(self):
        self.comment()

        with self.assertRaises(accounts.ForbiddenActionError):
            comments.list_comments(
                self.connection, self.admin, self.submission["id"]
            )
        with self.assertRaises(accounts.ForbiddenActionError):
            self.comment(actor=self.admin)

    def test_missing_and_foreign_submissions_are_hidden(self):
        for actor in (
            self.student,
            self.other_student,
            self.teacher,
            self.other_teacher,
        ):
            with self.subTest(role=actor.role):
                with self.assertRaises(submissions.MissingSubmissionError):
                    comments.list_comments(
                        self.connection, actor, uuid.uuid4().hex
                    )

    def test_invalid_actor_is_rejected(self):
        for actor in (None, "user", 7, object()):
            with self.subTest(actor=actor):
                with self.assertRaises(accounts.ForbiddenActionError):
                    comments.list_comments(
                        self.connection, actor, self.submission["id"]
                    )

    def test_malformed_submission_id_is_rejected_for_reads(self):
        for submission_id in ("", None, 7):
            with self.subTest(submission_id=submission_id):
                with self.assertRaises(comments.InvalidCommentRequestError):
                    comments.list_comments(
                        self.connection, self.student, submission_id
                    )

    def test_list_order_is_created_at_then_id(self):
        first_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        second_time = datetime(2026, 1, 2, tzinfo=timezone.utc)

        created = []
        created.append(self.comment(body="later", now=second_time)[1]["comment"])
        created.append(self.comment(body="same-a", now=first_time)[1]["comment"])
        created.append(self.comment(body="same-b", now=first_time)[1]["comment"])

        expected = sorted(
            created, key=lambda record: (record["createdAt"], record["id"])
        )
        listed = comments.list_comments(
            self.connection, self.student, self.submission["id"]
        )
        self.assertEqual(
            [comments.comment_to_mapping(item) for item in listed], expected
        )


class CommentIdempotencyTest(CommentTestBase):
    def test_exact_retry_replays_the_original_without_a_duplicate(self):
        key = uuid.uuid4().hex
        first = self.comment(key=key)
        second = self.comment(key=key)

        self.assertEqual(first, second)
        self.assertEqual(first[0], 201)
        self.assertEqual(len(self.comment_rows()), 1)

    def test_changed_body_with_the_same_key_conflicts(self):
        key = uuid.uuid4().hex
        self.comment(body="one", key=key)

        with self.assertRaises(idempotency.IdempotencyConflict):
            self.comment(body="two", key=key)
        self.assertEqual(len(self.comment_rows()), 1)

    def test_failed_create_records_no_result(self):
        key = uuid.uuid4().hex
        other_teacher = self.create_user("teacher")
        classes.change_class_owner(
            self.connection, self.admin, self.klass.id, other_teacher.id
        )

        with self.assertRaises(accounts.ForbiddenActionError):
            self.comment(actor=self.teacher, key=key)

        classes.change_class_owner(
            self.connection, self.admin, self.klass.id, self.teacher.id
        )
        status, _ = self.comment(actor=self.teacher, key=key)
        self.assertEqual(status, 201)
        self.assertEqual(len(self.comment_rows()), 1)


class InternalCommentTest(CommentTestBase):
    def test_internal_creation_requires_the_trusted_capability(self):
        for capability in (None, "capability", object(), self.teacher, dict):
            with self.subTest(capability=capability):
                with self.assertRaises(accounts.ForbiddenActionError):
                    self.internal_comment(capability=capability)
        self.assertEqual(self.comment_rows(), [])

    def test_internal_creation_rejects_teacher_and_arbitrary_sources(self):
        capability = comments._TRUSTED_COMMENT_SOURCE_CAPABILITY
        for source in ("teacher", "student", "", None, "AI", "admin"):
            with self.subTest(source=source):
                with self.assertRaises(comments.InvalidCommentRequestError):
                    self.internal_comment(capability=capability, source=source)
        self.assertEqual(self.comment_rows(), [])

    def test_internal_creation_records_ai_and_system_without_author(self):
        capability = comments._TRUSTED_COMMENT_SOURCE_CAPABILITY

        status, body = self.internal_comment(
            capability=capability, source="ai", body="AI note"
        )
        self.assertEqual(status, 201)
        self.assertIsNone(body["comment"]["authorUserId"])
        self.assertEqual(body["comment"]["source"], "ai")

        status, body = self.internal_comment(
            capability=capability,
            source="system",
            body="System note",
            key=uuid.uuid4().hex,
        )
        self.assertEqual(status, 201)
        self.assertIsNone(body["comment"]["authorUserId"])
        self.assertEqual(body["comment"]["source"], "system")

        rows = self.comment_rows()
        self.assertEqual({row["source"] for row in rows}, {"ai", "system"})
        self.assertTrue(all(row["author_user_id"] is None for row in rows))

    def test_internal_creation_is_idempotent(self):
        capability = comments._TRUSTED_COMMENT_SOURCE_CAPABILITY
        key = uuid.uuid4().hex

        first = self.internal_comment(
            capability=capability, source="ai", key=key
        )
        second = self.internal_comment(
            capability=capability, source="ai", key=key
        )
        self.assertEqual(first, second)
        self.assertEqual(len(self.comment_rows()), 1)

        with self.assertRaises(idempotency.IdempotencyConflict):
            self.internal_comment(
                capability=capability,
                source="ai",
                body="changed",
                key=key,
            )
        self.assertEqual(len(self.comment_rows()), 1)

    def test_internal_creation_validates_arguments(self):
        capability = comments._TRUSTED_COMMENT_SOURCE_CAPABILITY

        with self.assertRaises(submissions.MissingSubmissionError):
            self.internal_comment(
                capability=capability, submission_id=uuid.uuid4().hex
            )

        for service_user_id in ("", None, 7):
            with self.subTest(service_user_id=service_user_id):
                with self.assertRaises(comments.InvalidCommentRequestError):
                    self.internal_comment(
                        capability=capability,
                        service_user_id=service_user_id,
                    )

        with self.assertRaises(accounts.MissingUserError):
            self.internal_comment(
                capability=capability, service_user_id="nobody"
            )

        with self.assertRaises(comments.InvalidCommentRequestError):
            self.internal_comment(capability=capability, body="")

        self.assertEqual(self.comment_rows(), [])


class CommentApiSurfaceTest(CommentTestBase):
    def test_comment_is_frozen(self):
        record = comments.Comment(
            id="c",
            submission_id="s",
            author_user_id="u",
            source="teacher",
            body="b",
            created_at="2026-01-01T00:00:00.000000Z",
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.body = "changed"

    def test_no_update_or_delete_api(self):
        for name in (
            "update_comment",
            "delete_comment",
            "patch_comment",
            "edit_comment",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(comments, name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
