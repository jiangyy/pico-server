"""Tests for the Work content read domain."""

import json
import os
import sys
import unittest
import uuid

# Allow ``import work_content`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import authorization  # noqa: E402
import submissions  # noqa: E402
import work_content  # noqa: E402
import works  # noqa: E402

import test_works  # noqa: E402


class WorkContentTest(test_works.WorkTestBase):
    _DEFAULT = object()

    def setUp(self):
        super().setUp()
        self.playground_id = "pg-work-content"
        self.create_playground(self.admin, self.playground_id)
        self.make_public(self.playground_id)
        published = self.publish(self.admin, self.playground_id)
        self.playground_commit_id = published.commit_id
        self.assign_student(self.student)
        self.release = self.release_for(
            self.klass.id, self.playground_id, self.playground_commit_id
        )
        self.work = works.create_work_commit(
            self.connection,
            self.student,
            self.repository,
            self.playground_id,
            self.playground_commit_id,
            self.work_files(
                {
                    "work/main.py": b"print('work')\n",
                    "work/empty.bin": b"",
                    "work/data.bin": b"\x00\xff",
                }
            ),
        )
        self.work_commit_id = self.work.commit_id

    # ----- helpers ------------------------------------------------------

    def manifest(self, actor=_DEFAULT, playground_id=None, commit_id=None):
        return work_content.get_work_manifest(
            self.connection,
            self.student if actor is self._DEFAULT else actor,
            self.repository,
            self.student.id,
            self.playground_id if playground_id is None else playground_id,
            self.work_commit_id if commit_id is None else commit_id,
        )

    def read(self, path, actor=_DEFAULT):
        return work_content.read_work_file(
            self.connection,
            self.student if actor is self._DEFAULT else actor,
            self.repository,
            self.student.id,
            self.playground_id,
            self.work_commit_id,
            path,
        )

    def submit(self):
        status, body = submissions.create_submission(
            self.connection,
            self.student,
            self.repository,
            self.release["id"],
            self.work_commit_id,
            uuid.uuid4().hex,
        )
        self.assertEqual(status, 201)
        return body["submission"]

    def crafted_work_commit(self, extra=None, pin=None):
        metadata = json.dumps(
            {
                "playgroundCommitId": (
                    self.playground_commit_id if pin is None else pin
                )
            }
        ).encode("utf-8")
        files = {
            "picopg.json": test_works.MANIFEST_BYTES,
            "course/module/app.py": b"print(1)",
            ".picopg/work.json": metadata,
            "work/main.py": b"print('work')",
        }
        if extra:
            files.update(extra)
        return self.commit_tree(files)

    def point_work_ref(self, commit_id):
        self.git(
            "update-ref",
            self.repository.work_ref(self.playground_id, self.student.id),
            commit_id,
        )

    # ----- success ------------------------------------------------------

    def test_owner_manifest_is_exact_and_ordered(self):
        manifest = self.manifest()
        self.assertEqual(manifest.playground_id, self.playground_id)
        self.assertEqual(manifest.work_commit_id, self.work_commit_id)
        self.assertEqual(
            manifest.playground_commit_id, self.playground_commit_id
        )
        self.assertEqual(
            [item.path for item in manifest.files],
            ["data.bin", "empty.bin", "main.py"],
        )
        by_path = {item.path: item for item in manifest.files}
        self.assertEqual(by_path["data.bin"].size, 2)
        self.assertEqual(by_path["empty.bin"].size, 0)
        self.assertEqual(len(by_path["main.py"].sha256), 64)
        import hashlib

        self.assertEqual(
            by_path["main.py"].sha256,
            hashlib.sha256(b"print('work')\n").hexdigest(),
        )

    def test_owner_reads_exact_binary_and_empty_files(self):
        self.assertEqual(self.read("data.bin").data, b"\x00\xff")
        self.assertEqual(self.read("empty.bin").data, b"")
        self.assertEqual(self.read("main.py").data, b"print('work')\n")

    def test_teacher_with_matching_submission_can_read(self):
        self.submit()
        manifest = self.manifest(actor=self.teacher)
        self.assertEqual(manifest.work_commit_id, self.work_commit_id)
        self.assertEqual(
            self.read("main.py", actor=self.teacher).data, b"print('work')\n"
        )

    # ----- authorization -------------------------------------------------

    def test_other_student_admin_and_anonymous_are_denied(self):
        for actor in (self.other_student, self.admin, None):
            with self.subTest(actor=actor):
                with self.assertRaises(
                    authorization.ContentAccessDeniedError
                ):
                    self.manifest(actor=actor)

    def test_teacher_without_submission_is_denied(self):
        unrelated = self.create_user("teacher")
        with self.assertRaises(authorization.ContentAccessDeniedError):
            self.manifest(actor=unrelated)

    def test_wrong_playground_or_student_is_denied(self):
        with self.assertRaises(authorization.ContentAccessDeniedError):
            self.manifest(playground_id="pg-missing")
        with self.assertRaises(authorization.ContentAccessDeniedError):
            work_content.get_work_manifest(
                self.connection,
                self.student,
                self.repository,
                self.other_student.id,
                self.playground_id,
                self.work_commit_id,
            )

    def test_unreachable_commit_is_denied(self):
        with self.assertRaises(authorization.ContentAccessDeniedError):
            self.manifest(commit_id="0" * 40)

    # ----- malformed trees ----------------------------------------------

    def test_extra_top_level_path_is_rejected(self):
        commit = self.crafted_work_commit({"secrets/keys.txt": b"nope"})
        self.point_work_ref(commit)
        with self.assertRaises(work_content.WorkContentError) as caught:
            self.manifest(commit_id=commit)
        self.assertEqual(
            caught.exception.reason, work_content.REASON_WORK_TREE_INVALID
        )

    def test_non_portable_work_path_is_rejected(self):
        commit = self.crafted_work_commit({"work/CON.py": b"x"})
        self.point_work_ref(commit)
        with self.assertRaises(work_content.WorkContentError) as caught:
            self.manifest(commit_id=commit)
        self.assertEqual(
            caught.exception.reason, work_content.REASON_WORK_TREE_INVALID
        )

    def test_missing_metadata_is_rejected(self):
        commit = self.commit_tree(
            {
                "picopg.json": test_works.MANIFEST_BYTES,
                "course/module/app.py": b"print(1)",
                "work/main.py": b"print('work')",
            }
        )
        self.point_work_ref(commit)
        with self.assertRaises(work_content.WorkContentError) as caught:
            self.manifest(commit_id=commit)
        self.assertEqual(
            caught.exception.reason, work_content.REASON_WORK_METADATA_INVALID
        )

    def test_unpublished_pin_is_rejected(self):
        commit = self.crafted_work_commit(pin="0" * 40)
        self.point_work_ref(commit)
        with self.assertRaises(work_content.WorkContentError) as caught:
            self.manifest(commit_id=commit)
        self.assertEqual(
            caught.exception.reason,
            work_content.REASON_PLAYGROUND_COMMIT_INVALID,
        )

    def test_read_rejects_non_work_paths(self):
        for path in (
            ".picopg/work.json",
            "course/module/app.py",
            "starter/setup.py",
            "picopg.json",
            "/absolute.py",
            "work/main.py",
            "a/../b.py",
        ):
            with self.subTest(path=path):
                with self.assertRaises(work_content.WorkContentError) as caught:
                    self.read(path)
                self.assertEqual(
                    caught.exception.reason,
                    work_content.REASON_PATH_NOT_ALLOWED,
                )

    def test_read_of_absent_file_is_not_found(self):
        with self.assertRaises(work_content.WorkContentError) as caught:
            self.read("missing.py")
        self.assertEqual(
            caught.exception.reason, work_content.REASON_PATH_NOT_FOUND
        )

    def test_reads_do_not_mutate_the_work_ref(self):
        ref = self.repository.work_ref(self.playground_id, self.student.id)
        before = self.git("rev-parse", ref).strip()
        self.manifest()
        self.read("main.py")
        after = self.git("rev-parse", ref).strip()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
