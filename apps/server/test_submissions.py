"""Tests for immutable Submission records.

A submission is the one thing a client writes: a bounded file set recorded
against an exact published commit.  These tests drive the domain directly
against a real database and a real bare repository, so they check the rules a
request handler cannot: what is stored, what is refused, and what an exact
retry returns.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Allow ``import submissions`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bundles  # noqa: E402
from unittest import mock  # noqa: E402
import playgrounds  # noqa: E402
import repository  # noqa: E402
import submissions  # noqa: E402
import uuid  # noqa: E402
from database import immediate_transaction, open_database  # noqa: E402

MANIFEST_BYTES = b'{"schemaVersion":1,"runtime":"python"}'


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
        """One hand-in body, in the shape a client sends it."""

        payload = {
            "submission_id": submission_id(),
            "source_kind": "playground",
            "source_id": self.playground_id,
            "version_id": self.commit_id,
            "files": {"work/main.py": "# 姓名：2026-001\nprint(1)\n"},
        }
        payload.update(overrides)
        return payload

    def store(self, student="2026-001", files=None, submission_id=None):
        """Store one hand-in the way the endpoint does, and return the record."""

        files = files if files is not None else {"work/main.py": f"# 姓名：{student}\nprint(1)\n"}
        delivery = submissions.decode_delivery(
            {
                "submission_id": submission_id or globals()["submission_id"](),
                "source_kind": "playground",
                "source_id": self.playground_id,
                "version_id": self.commit_id,
                "files": files,
            }
        )
        with immediate_transaction(self.connection):
            write = submissions.create_if_absent(self.connection, delivery)
        self.assertNotEqual(write.status, "conflict")
        return {
            "id": delivery.submission_id,
            "source_kind": delivery.source_kind,
            "source_id": delivery.source_id,
            "version_id": delivery.version_id,
            "student": delivery.student,
            "created_at": write.created_at,
            "status": write.status,
        }


def submission_id():
    """A fresh delivery name. Each hand-in the tests store is its own delivery."""

    return uuid.uuid4().hex


class StudentFromFilesTest(unittest.TestCase):
    """The attributed header a hand-in carries in ``work/main.py``.

    This is the only place a student name exists: the request has no ``student``
    field, so the server reads the attribution out of the bytes the student
    wrote and files the work under that.
    """

    def student(self, line):
        return submissions.student_from_files({"work/main.py": f"{line}\nprint(1)\n"})

    def test_a_filled_header_names_the_student(self):
        self.assertEqual(self.student("# 姓名：张三"), "张三")
        self.assertEqual(self.student("# 姓名：张三 班级：1 班"), "张三（1 班）")

    def test_the_labels_may_appear_in_any_order(self):
        self.assertEqual(
            self.student("# 班级：1 班 姓名：张三"), "张三（1 班）"
        )

    def test_the_header_may_sit_under_blank_lines(self):
        self.assertEqual(self.student("\n\n# 姓名：张三"), "张三")

    def test_no_header_is_not_a_student(self):
        self.assertIsNone(self.student("print(1)"))
        self.assertIsNone(self.student("# 姓名："))
        self.assertIsNone(
            submissions.student_from_files({"work/other.py": "# 姓名：张三"})
        )
        self.assertIsNone(submissions.student_from_files({}))
        self.assertIsNone(submissions.student_from_files(None))

    def test_an_unusable_name_is_not_a_student(self):
        self.assertIsNone(self.student("# 姓名：" + "a" * 200))


class DecodeSubmissionTest(SubmissionTestBase):
    """What a request body must look like before anything is stored."""

    def test_a_valid_body_decodes_into_a_normalized_request(self):
        body = self.payload()
        request = submissions.decode_delivery(body)
        self.assertIsInstance(request, submissions.SubmissionDelivery)

        self.assertIsInstance(request, submissions.SubmissionDelivery)
        self.assertEqual(request.source_kind, "playground")
        self.assertEqual(request.source_id, self.playground_id)
        self.assertEqual(request.version_id, self.commit_id)
        self.assertEqual(request.submission_id, body["submission_id"])
        self.assertEqual(request.student, "2026-001")
        self.assertEqual(
            dict(request.files), {"work/main.py": "# 姓名：2026-001\nprint(1)\n"}
        )

    def test_the_student_is_read_out_of_the_files_not_the_body(self):
        # 请求里根本没有 student 字段：名字只存在于交上来的那份文件里。
        body = self.payload()
        self.assertNotIn("student", body)
        self.assertEqual(submissions.decode_delivery(body).student, "2026-001")

        with self.assertRaises(submissions.InvalidSubmissionRequestError):
            submissions.decode_delivery(
                self.payload(files={"work/main.py": "print(1)\n"})
            )

    def test_the_version_must_be_a_full_object_id(self):
        for version_id in ("HEAD", "abc", "0" * 39, "A" * 40, None):
            with self.subTest(version_id=version_id):
                with self.assertRaises(submissions.InvalidSubmissionRequestError):
                    submissions.decode_delivery(
                        self.payload(version_id=version_id)
                    )

    def test_the_delivery_name_is_bounded(self):
        for name in ("", None, 7, "a" * 65, "line\nbreak"):
            with self.subTest(name=name):
                with self.assertRaises(submissions.InvalidSubmissionRequestError):
                    submissions.decode_delivery(
                        self.payload(submission_id=name)
                    )

    def test_only_work_paths_may_be_handed_in(self):
        for path in ("course/index.html", "picopg.json", "work/../../x.py", "Work/a.py"):
            with self.subTest(path=path):
                with self.assertRaises(submissions.InvalidSubmissionRequestError):
                    submissions.decode_delivery(
                        self.payload(files={path: "x"})
                    )

    def test_a_case_colliding_file_set_is_rejected(self):
        with self.assertRaises(submissions.InvalidSubmissionRequestError):
            submissions.decode_delivery(
                self.payload(files={"work/A.py": "1", "work/a.py": "2"})
            )

    def test_files_must_be_text_and_bounded(self):
        with self.assertRaises(submissions.InvalidSubmissionRequestError):
            submissions.decode_delivery(self.payload(files={"work/a.py": 7}))

        with self.assertRaises(submissions.InvalidSubmissionRequestError):
            submissions.decode_delivery(self.payload(files={"work/a.py": "x" * (1024 * 1024 + 1)}))

    def test_shape_errors_are_rejected(self):
        for payload in (None, [], "text", {}, {"source_id": "pg-one"}):
            with self.subTest(payload=payload):
                with self.assertRaises(submissions.InvalidSubmissionRequestError):
                    submissions.decode_delivery(payload)

    def test_a_file_set_with_no_attribution_is_refused(self):
        # 名字只存在于交上来的文件里：没有文件（或文件里没有表头）就没有这份作业
        # 该记在谁名下，那不是"无名"，是"不能收"。
        for files in ({}, {"work/other.py": "# 姓名：2026-001"}):
            with self.subTest(files=files):
                with self.assertRaises(submissions.InvalidSubmissionRequestError):
                    submissions.decode_delivery(self.payload(files=files))


class FileSetValidationTest(unittest.TestCase):
    """The one place a client contributes bytes: which file sets are refused.

    Each case names the exact reason the API reports, because that string is
    what a caller gets in `details.reason` and nothing else.
    """

    def reason(self, files):
        with self.assertRaises(submissions.InvalidSubmissionRequestError) as caught:
            submissions.decode_delivery(
                {
                    "submission_id": submission_id(),
                    "source_kind": "playground",
                    "source_id": "pg-one",
                    "version_id": "0" * 40,
                    "files": files,
                }
            )
        return str(caught.exception).split(": ", 1)[-1]

    def test_only_work_paths_are_accepted(self):
        for path, reason in [
            (".picopg/work.json", "path_not_allowed"),
            ("picopg.json", "path_not_allowed"),
            ("course/module/app.py", "path_not_allowed"),
            ("starter/setup.py", "path_not_allowed"),
            ("work", "path_not_allowed"),
            ("Work/main.py", "path_not_allowed"),
            ("work/../escape.py", "path_dot_segment"),
            ("work/./same.py", "path_dot_segment"),
            ("work//empty.py", "path_empty_segment"),
            ("/work/absolute.py", "path_absolute"),
            ("work\\windows.py", "path_backslash"),
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.reason({path: "x"}), reason)

    def test_non_portable_paths_are_refused_with_the_bundle_reason(self):
        for path, reason in [
            ("work/CON.py", "path_reserved_name"),
            ("work/sub/LPT1.txt", "path_reserved_name"),
            ("work/.git/config", "path_reserved_name"),
            ("work/a:b.py", "path_portable_character"),
            ("work/trailing.", "path_portable_character"),
            ("work/trailing ", "path_portable_character"),
            ("work/a\nb.py", "path_control"),
            ("work/a\u0001b.py", "path_control"),
            ("work/a\u007fb.py", "path_control"),
            ("work/a\ud800b", "path_invalid_unicode"),
            ("work/x/a\x00.py", "path_nul"),
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.reason({path: "x"}), reason)

    def test_portable_near_misses_are_accepted(self):
        request = submissions.decode_delivery(
            {
                "submission_id": submission_id(),
                "source_kind": "playground",
                "source_id": "pg-one",
                "version_id": "0" * 40,
                "files": {
                    "work/main.py": "# 姓名：2026-001\n",
                    "work/console.py": "1",
                    "work/com10.txt": "2",
                    "work/a b.txt": "3",
                    "work/café.py": "4",
                },
            }
        )
        self.assertEqual(
            sorted(request.files),
            [
                "work/a b.txt",
                "work/café.py",
                "work/com10.txt",
                "work/console.py",
                "work/main.py",
            ],
        )

    def test_a_case_colliding_set_is_refused(self):
        self.assertEqual(
            self.reason({"work/File.py": "1", "work/file.py": "2"}),
            "path_case_collision",
        )

    def test_two_paths_that_normalize_alike_are_refused(self):
        # JSON can carry the decomposed and the composed form of one name; only
        # one of them may become the stored path.
        self.assertEqual(
            self.reason({"work/caf\u00e9.py": "1", "work/cafe\u0301.py": "2"}),
            "duplicate_path",
        )

    def test_a_file_path_that_is_not_text_is_refused(self):
        self.assertEqual(self.reason({"work/a.py": 7}), "entry_data_not_bytes")

    def test_the_limits_are_the_bundle_limits(self):
        import bundles

        too_many = {f"work/f{index}.py": "x" for index in range(bundles.MAX_ENTRY_COUNT + 1)}
        self.assertEqual(self.reason(too_many), "too_many_entries")

        with mock.patch.object(bundles, "MAX_FILE_BYTES", 3):
            self.assertEqual(self.reason({"work/a.py": "1234"}), "file_too_large")

        with mock.patch.object(bundles, "MAX_TOTAL_BYTES", 3):
            self.assertEqual(
                self.reason({"work/a.py": "12", "work/b.py": "34"}), "work_too_large"
            )

    def test_the_file_set_is_returned_canonically(self):
        request = submissions.decode_delivery(
            {
                "submission_id": submission_id(),
                "source_kind": "playground",
                "source_id": "pg-one",
                "version_id": "0" * 40,
                "files": {"work/b.py": "2", "work/a.py": "1", "work/main.py": "# 姓名：2026-001\n"},
            }
        )
        self.assertEqual(
            list(request.files.items()),
            [("work/a.py", "1"), ("work/b.py", "2"), ("work/main.py", "# 姓名：2026-001\n")],
        )


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
                "INSERT INTO submissions (id, source_kind, source_id,"
                " version_id, student, files_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("sub-1", "playground", self.playground_id, "HEAD", "a", "{}", "2026-01-01T00:00:00Z"),
            )
        with self.assertRaises(submissions.StoredSubmissionError):
            submissions.get_submission(self.connection, "sub-1")

    def test_a_malformed_files_column_fails_closed(self):
        with self.connection:
            self.connection.execute(
                "INSERT INTO submissions (id, source_kind, source_id,"
                " version_id, student, files_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("sub-2", "playground", self.playground_id, self.commit_id, "a", "[]", "x"),
            )
        with self.assertRaises(submissions.StoredSubmissionError):
            submissions.get_submission(self.connection, "sub-2")

    def test_listing_is_newest_first_and_paged(self):
        for index in range(3):
            self.store(student=f"2026-00{index}")

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
            self.connection,
            self.store(files={"work/main.py": "# 姓名：2026-001\nprint(1)\n"})["id"],
        )

        self.assertNotIn(
            "files", submissions.submission_to_mapping(stored.submission)
        )
        self.assertEqual(
            submissions.submission_to_mapping(stored.submission, files=stored.files)[
                "files"
            ],
            [{"path": "work/main.py", "text": "# 姓名：2026-001\nprint(1)\n"}],
        )


class BackfillSubmissionFilesTest(unittest.TestCase):
    """补齐：记录在库里，文件夹只是它的副本，所以可以再写一遍。"""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        base = Path(self.temporary_directory.name)
        self.connection = open_database(base / "app.db")
        self.addCleanup(self.connection.close)
        self.root = base / "submissions"
        with self.connection:
            self.connection.execute(
                "INSERT INTO playgrounds (id, created_at) VALUES ('pg-one', '2026-01-01T00:00:00Z')"
            )

    def store(self, submission_id, student, text="print(1)\n", created_at="2026-09-18T10:11:12Z"):
        with self.connection:
            self.connection.execute(
                "INSERT INTO submissions (id, source_kind, source_id, version_id, student,"
                " files_json, created_at) VALUES (?, 'playground', 'pg-one', ?, ?, ?, ?)",
                (submission_id, "c1" + "0" * 38, student, json.dumps({"work/main.py": text}), created_at),
            )

    def test_a_stored_submission_without_a_folder_gets_one(self):
        self.store("s1", "张三（1 班）")
        self.assertEqual(submissions.backfill_submission_files(self.connection, self.root), 1)
        self.assertEqual(
            (self.root / "1 班" / "张三" / "pg-one" / "work" / "main.py").read_text(encoding="utf-8"),
            "print(1)\n",
        )

    def test_it_is_idempotent(self):
        self.store("s1", "张三（1 班）")
        self.assertEqual(submissions.backfill_submission_files(self.connection, self.root), 1)
        self.assertEqual(submissions.backfill_submission_files(self.connection, self.root), 0)

    def test_each_of_a_students_hand_ins_gets_its_own_numbered_folder(self):
        self.store("s1", "李四", "print(1)\n", "2026-09-18T10:00:00Z")
        self.store("s2", "李四", "print(2)\n", "2026-09-18T11:00:00Z")
        self.assertEqual(submissions.backfill_submission_files(self.connection, self.root), 2)
        base = self.root / "李四"
        self.assertEqual(sorted(p.name for p in base.iterdir()), ["pg-one", "pg-one-2"])
        self.assertEqual((base / "pg-one-2" / "work" / "main.py").read_text(encoding="utf-8"), "print(2)\n")

    def test_a_half_written_folder_is_repaired(self):
        # 没有 submission.json 就说明上次没写完（可能是断电），要重写
        self.store("s1", "王五")
        partial = self.root / "王五" / "pg-one" / "work"
        partial.mkdir(parents=True)
        (partial / "main.py").write_text("half", encoding="utf-8")

        self.assertEqual(submissions.backfill_submission_files(self.connection, self.root), 1)
        self.assertEqual((partial / "main.py").read_text(encoding="utf-8"), "print(1)\n")
        self.assertTrue((self.root / "王五" / "pg-one" / submissions.MANIFEST_NAME).is_file())

    def test_an_unreadable_row_is_skipped_not_fatal(self):
        self.store("s1", "张三")
        with self.connection:
            self.connection.execute("UPDATE submissions SET files_json = 'not json' WHERE id = 's1'")
        self.assertEqual(submissions.backfill_submission_files(self.connection, self.root), 0)


class SubmissionFileLayoutTest(unittest.TestCase):
    """Where a handed-in file set lands on disk.

    The student string is the only caller-controlled part of that path, so
    these tests are as much about what a hostile name cannot do as about the
    layout a teacher reads.
    """

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "submissions"

    def write(self, student, files=None, ordinal=1):
        return submissions.write_submission_files(
            root=self.root,
            submission_id="abcdef1234567890",
            student=student,
            source_id="pg-one",
            version_id="c1" + "0" * 38,
            created_at="2026-09-18T10:11:12Z",
            files=files if files is not None else {"work/main.py": "print(1)\n"},
            ordinal=ordinal,
        )

    def test_class_and_name_become_one_directory_each(self):
        directory = self.write("张三（1 班）")
        self.assertEqual(directory.relative_to(self.root), Path("1 班") / "张三" / "pg-one")
        self.assertEqual((directory / "work/main.py").read_text(encoding="utf-8"), "print(1)\n")

    def test_a_name_without_a_class_is_filed_directly_under_it(self):
        self.assertEqual(
            self.write("张三").relative_to(self.root), Path("张三") / "pg-one"
        )

    def test_handing_the_same_course_in_again_is_numbered(self):
        first = self.write("张三（1 班）", ordinal=1)
        second = self.write("张三（1 班）", ordinal=2)
        third = self.write("张三（1 班）", ordinal=3)
        self.assertEqual(
            [d.name for d in (first, second, third)], ["pg-one", "pg-one-2", "pg-one-3"]
        )
        self.assertEqual(first.parent, second.parent, "同一个学生的同一门课在同一目录下")

    def test_the_folder_says_whose_work_it_is(self):
        directory = self.write("张三（1 班）")
        manifest = json.loads((directory / submissions.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(
            manifest,
            {
                "versionId": "c1" + "0" * 38,
                "createdAt": "2026-09-18T10:11:12Z",
                "files": ["work/main.py"],
                "id": "abcdef1234567890",
                "ordinal": 1,
                "sourceId": "pg-one",
                "student": "张三（1 班）",
            },
        )

    def test_a_name_cannot_reach_outside_the_root(self):
        for hostile in ("../../etc/passwd", "..", "/etc/passwd", "a/b", "a\\b", "..（..）"):
            with self.subTest(hostile=hostile):
                directory = self.write(hostile)
                self.assertIn(self.root, directory.parents)
                for part in directory.relative_to(self.root).parts:
                    self.assertNotIn("/", part)
                    self.assertNotIn("\\", part)
                    self.assertNotIn(part, ("", ".", ".."))

    def test_a_nameless_student_still_gets_a_directory(self):
        # 一个只剩点号的名字不能产生无名目录：目录名是老师唯一的抓手。
        self.assertIn("student", self.write("...").relative_to(self.root).parts)

    def test_a_long_name_is_truncated_per_segment(self):
        directory = self.write(f"{'长' * 200}（{'班' * 200}）")
        for part in directory.relative_to(self.root).parts:
            self.assertLessEqual(len(part), submissions.MAX_SEGMENT_LENGTH)


if __name__ == "__main__":
    unittest.main(verbosity=2)
