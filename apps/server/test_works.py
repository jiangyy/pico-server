"""Tests for Server-owned Work commit creation.

The tests build real public/protected Playground history, a real Class,
Release, and database, then exercise the Work parent rules, the exact assembled
tree, server-owned metadata, ownership, authorization, path rejection, limits,
and the concurrent ref change guard.
"""

import base64
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from unittest import mock

# Allow ``import works`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import accounts  # noqa: E402
import authorization  # noqa: E402
import bundles  # noqa: E402
import classes  # noqa: E402
import playgrounds  # noqa: E402
import releases  # noqa: E402
import repository  # noqa: E402
import works  # noqa: E402
from database import open_database  # noqa: E402


PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)
MANIFEST_BYTES = b'{"schemaVersion":1,"runtime":"python"}'


class WorkTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        base = Path(self.temporary_directory.name)
        self.connection = open_database(base / "app.db")
        self.addCleanup(self.connection.close)
        self.repository_path = base / "picopg.git"
        self.repository = repository.open_repository(self.repository_path)

        bootstrap = accounts.User(
            id="seed-actor",
            username="seed-actor",
            role="admin",
            created_at="2026-01-01T00:00:00Z",
        )
        self.admin = accounts.create_user(
            self.connection,
            bootstrap,
            f"admin-{uuid.uuid4().hex[:8]}",
            "secret",
            "admin",
        )
        self.teacher = self.create_user("teacher")
        self.student = self.create_user("student")
        self.other_student = self.create_user("student")
        self.klass = classes.create_class(
            self.connection, self.admin, "Class", self.teacher.id
        )

    # ----- database helpers ---------------------------------------------

    def create_user(self, role):
        return accounts.create_user(
            self.connection,
            self.admin,
            f"{role}-{uuid.uuid4().hex[:8]}",
            "secret",
            role,
        )

    def assign_student(self, student):
        return classes.assign_student(
            self.connection, self.admin, student.id, self.klass.id
        )

    def create_playground(self, creator, playground_id):
        """Create Playground metadata; management is Admin-only."""
        return playgrounds.create_playground(
            self.connection, self.admin, playground_id
        )

    def make_public(self, playground_id):
        return playgrounds.update_playground(
            self.connection, self.admin, playground_id, {"policy": "public"}
        )

    def make_bundle(self, course_data=b"print(1)"):
        return bundles.validate_bundle(
            [
                bundles.BundleEntry("picopg.json", MANIFEST_BYTES),
                bundles.BundleEntry("course/module/app.py", course_data),
                bundles.BundleEntry("starter/setup.py", b"setup"),
            ]
        )

    def metadata(self, message="publish playground"):
        return repository.CommitMetadata(
            name="Test Author",
            email="author@example.test",
            timestamp="2026-01-01T00:00:00+00:00",
            message=message,
        )

    def publish(self, actor, playground_id, course_data=b"print(1)"):
        """Publish as Admin; Playground publishing is Admin-only."""
        return playgrounds.publish_playground(
            self.connection,
            self.admin,
            playground_id,
            self.repository,
            self.make_bundle(course_data),
            self.metadata(f"publish {playground_id}"),
        )

    def release_for(self, class_id, playground_id, commit_id):
        status, body = releases.create_release(
            self.connection,
            self.teacher,
            self.repository,
            class_id,
            playground_id,
            commit_id,
            PAST,
            False,
            uuid.uuid4().hex,
        )
        self.assertEqual(status, 201)
        return body["release"]

    # ----- Git helpers ---------------------------------------------------

    def git(self, *arguments, input=None, environment=None, check=True):
        effective = dict(os.environ)
        if environment is not None:
            effective.update(environment)
        completed = subprocess.run(
            ["git", "-C", str(self.repository_path), *arguments],
            input=input,
            capture_output=True,
            env=effective,
        )
        if check:
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr.decode("utf-8", "replace"),
            )
        return completed.stdout

    def commit_tree(self, files, message=b"crafted\n", parent=None):
        root = {}
        for path, data in files.items():
            node = root
            for part in path.split("/")[:-1]:
                node = node.setdefault(part, {})
            node[path.split("/")[-1]] = data
        tree = self._write_tree_node(root)
        arguments = ["commit-tree", tree]
        if parent is not None:
            arguments += ["-p", parent]
        environment = {
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.test",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "author@example.test",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
        }
        return (
            self.git(*arguments, input=message, environment=environment)
            .strip()
            .decode("ascii")
        )

    def _write_tree_node(self, node):
        lines = []
        for name in sorted(node):
            value = node[name]
            if isinstance(value, dict):
                object_id = self._write_tree_node(value)
                lines.append(f"040000 tree {object_id}\t{name}")
            else:
                blob = (
                    self.git("hash-object", "-w", "--stdin", input=value)
                    .strip()
                    .decode("ascii")
                )
                lines.append(f"100644 blob {blob}\t{name}")
        return (
            self.git("mktree", input=("\n".join(lines) + "\n").encode())
            .strip()
            .decode("ascii")
        )

    # ----- Work helpers --------------------------------------------------

    def work_files(self, files):
        return works.validate_work_files(
            [works.WorkEntry(path, data) for path, data in files.items()]
        )

    def create_work(
        self, actor=None, files=None, base_commit_id=None, playground_id=None,
        playground_commit_id=None,
    ):
        actor = self.student if actor is None else actor
        if files is None:
            files = {"work/main.py": b"print('work')"}
        return works.create_work_commit(
            self.connection,
            actor,
            self.repository,
            playground_id,
            playground_commit_id,
            self.work_files(files),
            base_commit_id=base_commit_id,
        )

    def ref_exists(self, ref):
        completed = subprocess.run(
            [
                "git", "-C", str(self.repository_path),
                "rev-parse", "--verify", "--quiet", ref,
            ],
            capture_output=True,
        )
        return completed.returncode == 0

    def tree_paths(self, commit_id):
        raw = self.git("ls-tree", "-r", "-z", "--name-only", commit_id)
        return [name.decode("utf-8") for name in raw.split(b"\x00") if name]

    def file_bytes(self, commit_id, path):
        return self.git("show", f"{commit_id}:{path}")

    def parents(self, commit_id):
        return [
            value.decode("ascii")
            for value in self.git(
                "rev-list", "--parents", "-n", "1", commit_id
            ).split()[1:]
        ]

    def work_count(self, ref, playground_commit_id):
        """Number of Work commits on the ref, excluding the Playground base."""

        return self.git(
            "rev-list", "--count", ref, f"^{playground_commit_id}"
        ).strip()

    def assert_content_not_read(self):
        def forbidden(*arguments, **kwargs):
            raise AssertionError("Playground content was read")

        tree_patcher = mock.patch.object(
            works.repository_module, "list_commit_tree", forbidden
        )
        blob_patcher = mock.patch.object(
            works.repository_module, "read_blobs", forbidden
        )
        tree_patcher.start()
        blob_patcher.start()
        self.addCleanup(tree_patcher.stop)
        self.addCleanup(blob_patcher.stop)


class WorkCreationTest(WorkTestBase):
    def setUp(self):
        super().setUp()
        self.playground_id = "pg-work"
        self.create_playground(self.teacher, self.playground_id)
        self.first_playground = self.publish(
            self.teacher, self.playground_id, b"print(1)"
        )
        self.assign_student(self.student)
        self.release_for(
            self.klass.id, self.playground_id, self.first_playground.commit_id
        )

    # ----- parent and tree rules ----------------------------------------

    def test_first_commit_parent_is_the_exact_playground_commit(self):
        result = self.create_work(
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        self.assertIsNone(result.base_commit_id)
        self.assertEqual(result.student_id, self.student.id)
        self.assertEqual(
            self.parents(result.commit_id),
            [self.first_playground.commit_id],
        )

        ref = self.repository.work_ref(self.playground_id, self.student.id)
        self.assertEqual(
            self.git("rev-parse", ref).strip().decode("ascii"),
            result.commit_id,
        )
        self.assertEqual(
            self.work_count(ref, self.first_playground.commit_id), b"1"
        )

    def test_later_commit_parent_is_current_work_head(self):
        first = self.create_work(
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        second = self.create_work(
            base_commit_id=first.commit_id,
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        self.assertEqual(second.base_commit_id, first.commit_id)
        self.assertEqual(self.parents(second.commit_id), [first.commit_id])

        third = self.create_work(
            base_commit_id=second.commit_id,
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
            files={"work/main.py": b"third"},
        )
        self.assertEqual(self.parents(third.commit_id), [second.commit_id])

        ref = self.repository.work_ref(self.playground_id, self.student.id)
        self.assertEqual(
            self.work_count(ref, self.first_playground.commit_id), b"3"
        )

    def test_assembled_tree_is_exactly_playground_plus_work_plus_manifest(self):
        result = self.create_work(
            files={"work/main.py": b"print('work')", "work/pkg/util.py": b"u"},
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        paths = sorted(self.tree_paths(result.commit_id))
        self.assertEqual(
            paths,
            [
                ".picopg/work.json",
                "course/module/app.py",
                "picopg.json",
                "starter/setup.py",
                "work/main.py",
                "work/pkg/util.py",
            ],
        )
        # Playground content is copied byte-for-byte from the exact commit.
        self.assertEqual(
            self.file_bytes(result.commit_id, "course/module/app.py"),
            self.file_bytes(
                self.first_playground.commit_id, "course/module/app.py"
            ),
        )
        self.assertEqual(
            self.file_bytes(result.commit_id, "starter/setup.py"),
            self.file_bytes(
                self.first_playground.commit_id, "starter/setup.py"
            ),
        )
        self.assertEqual(
            self.file_bytes(result.commit_id, "picopg.json"), MANIFEST_BYTES
        )
        self.assertEqual(
            self.file_bytes(result.commit_id, "work/pkg/util.py"), b"u"
        )

    def test_manifest_pins_the_requested_playground_commit(self):
        result = self.create_work(
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        manifest = self.file_bytes(result.commit_id, ".picopg/work.json")
        self.assertEqual(
            manifest,
            (
                '{"playgroundCommitId":"'
                + self.first_playground.commit_id
                + '"}'
            ).encode("utf-8"),
        )

    def test_requested_commit_is_used_after_playground_head_advances(self):
        second = self.publish(self.teacher, self.playground_id, b"print(2)")
        self.assertNotEqual(
            second.commit_id, self.first_playground.commit_id
        )

        result = self.create_work(
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        # The pinned commit, not the Playground HEAD, is the parent and source.
        self.assertEqual(
            self.parents(result.commit_id),
            [self.first_playground.commit_id],
        )
        self.assertEqual(
            self.file_bytes(result.commit_id, "course/module/app.py"),
            b"print(1)",
        )
        manifest = self.file_bytes(result.commit_id, ".picopg/work.json")
        self.assertIn(self.first_playground.commit_id.encode("ascii"), manifest)
        self.assertNotIn(second.commit_id.encode("ascii"), manifest)

    def test_empty_work_snapshot_is_allowed(self):
        result = self.create_work(
            files={},
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        self.assertIn(".picopg/work.json", self.tree_paths(result.commit_id))

    # ----- metadata ownership -------------------------------------------

    def test_commit_identity_and_message_are_server_owned(self):
        result = self.create_work(
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        author = (
            self.git("log", "--format=%an <%ae>", "-n", "1", result.commit_id)
            .decode("utf-8")
            .strip()
        )
        self.assertEqual(
            author, f"{works.SERVER_COMMIT_NAME} <{works.SERVER_COMMIT_EMAIL}>"
        )
        message = (
            self.git("log", "--format=%s", "-n", "1", result.commit_id)
            .decode("utf-8")
            .strip()
        )
        self.assertEqual(message, works.SERVER_COMMIT_MESSAGE)

    def test_only_the_derived_work_ref_reaches_git(self):
        seen = []
        original = repository._run_git

        def spy(arguments, cwd=None, stdin=None, environment=None):
            seen.append(tuple(arguments))
            return original(
                arguments, cwd=cwd, stdin=stdin, environment=environment
            )

        with mock.patch.object(repository, "_run_git", side_effect=spy):
            self.create_work(
                playground_id=self.playground_id,
                playground_commit_id=self.first_playground.commit_id,
            )

        expected_ref = self.repository.work_ref(
            self.playground_id, self.student.id
        )
        playground_ref = self.repository.playground_ref(self.playground_id)
        for arguments in seen:
            for argument in arguments:
                if "refs/" in argument:
                    self.assertIn(argument, {expected_ref, playground_ref})
                self.assertNotIn("course/", argument)
                self.assertNotIn("starter/", argument)

    def test_work_refs_are_isolated_per_student(self):
        self.assign_student(self.other_student)
        self.create_work(
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        mine = self.repository.work_ref(self.playground_id, self.student.id)
        theirs = self.repository.work_ref(
            self.playground_id, self.other_student.id
        )
        self.assertTrue(self.ref_exists(mine))
        self.assertFalse(self.ref_exists(theirs))

    # ----- submitted path rejection -------------------------------------

    def test_client_cannot_submit_playground_or_server_paths(self):
        forbidden = [
            (".picopg/work.json", works.REASON_PATH_NOT_ALLOWED),
            (".picopg/extra.json", works.REASON_PATH_NOT_ALLOWED),
            ("picopg.json", works.REASON_PATH_NOT_ALLOWED),
            ("course/module/app.py", works.REASON_PATH_NOT_ALLOWED),
            ("starter/setup.py", works.REASON_PATH_NOT_ALLOWED),
            ("work/../escape.py", works.REASON_PATH_DOT_SEGMENT),
            ("work/./same.py", works.REASON_PATH_DOT_SEGMENT),
            ("work//empty.py", works.REASON_PATH_EMPTY_SEGMENT),
            ("/work/absolute.py", works.REASON_PATH_ABSOLUTE),
            ("work\\windows.py", works.REASON_PATH_BACKSLASH),
            ("work", works.REASON_PATH_NOT_ALLOWED),
            ("Work/main.py", works.REASON_PATH_NOT_ALLOWED),
        ]
        for path, reason in forbidden:
            with self.subTest(path=path):
                with self.assertRaises(works.WorkValidationError) as caught:
                    works.validate_work_files(
                        [works.WorkEntry(path, b"x")]
                    )
                self.assertEqual(caught.exception.reason, reason)

    def test_invalid_path_shapes_and_entry_types_are_rejected(self):
        cases = [
            (works.WorkEntry(None, b"x"), works.REASON_PATH_NOT_STR),
            (works.WorkEntry("work/a\x00.py", b"x"), works.REASON_PATH_NUL),
            (works.WorkEntry("work/a.py", "text"), works.REASON_ENTRY_DATA_NOT_BYTES),
            ("not an entry", works.REASON_INVALID_ENTRY),
        ]
        for entry, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(works.WorkValidationError) as caught:
                    works.validate_work_files([entry])
                self.assertEqual(caught.exception.reason, reason)

    def test_control_character_paths_are_rejected(self):
        # A path with a C0 control character or DEL must be rejected by the
        # domain validator itself, not later by the repository tree writer.
        for path in ("work/a\nb.py", "work/a\x01b.py", "work/a\x7fb.py"):
            with self.subTest(path=path):
                with self.assertRaises(works.WorkValidationError) as caught:
                    works.validate_work_files([works.WorkEntry(path, b"x")])
                self.assertEqual(
                    caught.exception.reason,
                    works.REASON_PATH_CONTROL_CHARACTER,
                )
        self.assertIn(works.REASON_PATH_CONTROL_CHARACTER, works.REASON_CODES)

    def test_lone_surrogate_path_is_rejected(self):
        # A lone surrogate is not UTF-8 encodable; it must never reach the
        # repository tree writer (which would raise UnicodeEncodeError).
        for path in ("work/a\ud800b", "work/\udfff", "work/x/\ud800.py"):
            with self.subTest(path=path):
                with self.assertRaises(works.WorkValidationError) as caught:
                    works.validate_work_files([works.WorkEntry(path, b"x")])
                self.assertEqual(
                    caught.exception.reason,
                    works.REASON_PATH_INVALID_UNICODE,
                )
        self.assertIn(works.REASON_PATH_INVALID_UNICODE, works.REASON_CODES)

    def test_create_work_commit_rejects_surrogate_path_without_crashing(self):
        # Defense in depth: ``create_work_commit`` re-validates the mapping, so
        # a caller that bypasses ``validate_work_files`` still gets the domain
        # error instead of an unhandled UnicodeEncodeError.
        files = MappingProxyType({"work/a\ud800b": b"x"})
        with self.assertRaises(works.WorkValidationError) as caught:
            works.create_work_commit(
                self.connection,
                self.student,
                self.repository,
                self.playground_id,
                self.first_playground.commit_id,
                files,
            )
        self.assertEqual(
            caught.exception.reason, works.REASON_PATH_INVALID_UNICODE
        )
        ref = self.repository.work_ref(self.playground_id, self.student.id)
        self.assertFalse(self.ref_exists(ref))

    def test_duplicate_normalized_paths_are_rejected(self):
        with self.assertRaises(works.WorkValidationError) as caught:
            works.validate_work_files(
                [
                    works.WorkEntry("work/a.py", b"1"),
                    works.WorkEntry("work/a.py", b"2"),
                ]
            )
        self.assertEqual(caught.exception.reason, works.REASON_DUPLICATE_PATH)

    def test_work_snapshot_must_be_the_normalized_mapping(self):
        playground_commit_id = self.first_playground.commit_id
        with self.assertRaises(works.WorkValidationError) as caught:
            works.create_work_commit(
                self.connection,
                self.student,
                self.repository,
                self.playground_id,
                playground_commit_id,
                {"work/main.py": b"x"},
            )
        self.assertEqual(
            caught.exception.reason, works.REASON_WORK_NOT_NORMALIZED
        )

    # ----- limits --------------------------------------------------------

    def test_entry_count_limit(self):
        with mock.patch.object(works, "MAX_WORK_ENTRY_COUNT", 1):
            with self.assertRaises(works.WorkValidationError) as caught:
                works.validate_work_files(
                    [
                        works.WorkEntry("work/a.py", b"1"),
                        works.WorkEntry("work/b.py", b"2"),
                    ]
                )
        self.assertEqual(caught.exception.reason, works.REASON_TOO_MANY_ENTRIES)

    def test_per_file_size_limit(self):
        with mock.patch.object(works, "MAX_WORK_FILE_BYTES", 3):
            with self.assertRaises(works.WorkValidationError) as caught:
                works.validate_work_files(
                    [works.WorkEntry("work/a.py", b"1234")]
                )
        self.assertEqual(caught.exception.reason, works.REASON_FILE_TOO_LARGE)

    def test_total_work_size_limit(self):
        with mock.patch.object(works, "MAX_WORK_TOTAL_BYTES", 3):
            with self.assertRaises(works.WorkValidationError) as caught:
                works.validate_work_files(
                    [
                        works.WorkEntry("work/a.py", b"12"),
                        works.WorkEntry("work/b.py", b"34"),
                    ]
                )
        self.assertEqual(caught.exception.reason, works.REASON_WORK_TOO_LARGE)

    def test_limits_are_named_and_bounded(self):
        self.assertIsInstance(works.MAX_WORK_ENTRY_COUNT, int)
        self.assertIsInstance(works.MAX_WORK_FILE_BYTES, int)
        self.assertIsInstance(works.MAX_WORK_TOTAL_BYTES, int)
        self.assertGreater(works.MAX_WORK_ENTRY_COUNT, 0)
        self.assertGreater(works.MAX_WORK_FILE_BYTES, 0)
        self.assertGreaterEqual(
            works.MAX_WORK_TOTAL_BYTES, works.MAX_WORK_FILE_BYTES
        )

    # ----- ownership and authorization ----------------------------------

    def test_non_student_actor_is_denied_before_content_read(self):
        for actor in (self.teacher, self.admin):
            with self.subTest(role=actor.role):
                self.assert_content_not_read()
                with self.assertRaises(
                    authorization.ContentAccessDeniedError
                ):
                    self.create_work(
                        actor=actor,
                        playground_id=self.playground_id,
                        playground_commit_id=self.first_playground.commit_id,
                    )

    def test_student_without_release_is_denied_before_content_read(self):
        other = self.create_user("student")
        self.assert_content_not_read()
        with self.assertRaises(authorization.ContentAccessDeniedError):
            self.create_work(
                actor=other,
                playground_id=self.playground_id,
                playground_commit_id=self.first_playground.commit_id,
            )

    def test_denied_and_missing_targets_share_one_error(self):
        other = self.create_user("student")

        def denial(commit_id):
            with self.assertRaises(
                authorization.ContentAccessDeniedError
            ) as caught:
                self.create_work(
                    actor=other,
                    playground_id=self.playground_id,
                    playground_commit_id=commit_id,
                )
            return caught.exception

        missing = denial("a" * 40)
        unauthorized = denial(self.first_playground.commit_id)
        self.assertEqual(type(missing), type(unauthorized))
        self.assertEqual(str(missing), str(unauthorized))

    def test_commit_from_another_playground_is_denied(self):
        self.create_playground(self.teacher, "pg-other")
        other = self.publish(self.teacher, "pg-other", b"other")
        self.assert_content_not_read()
        with self.assertRaises(authorization.ContentAccessDeniedError):
            self.create_work(
                playground_id=self.playground_id,
                playground_commit_id=other.commit_id,
            )

    def test_student_cannot_append_to_another_students_work(self):
        first = self.create_work(
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        self.assign_student(self.other_student)

        with self.assertRaises(repository.WorkHeadChangedError):
            self.create_work(
                actor=self.other_student,
                base_commit_id=first.commit_id,
                playground_id=self.playground_id,
                playground_commit_id=self.first_playground.commit_id,
            )

        # The other Student's own ref stays absent and the owner's is intact.
        mine = self.repository.work_ref(self.playground_id, self.student.id)
        theirs = self.repository.work_ref(
            self.playground_id, self.other_student.id
        )
        self.assertEqual(
            self.git("rev-parse", mine).strip().decode("ascii"),
            first.commit_id,
        )
        self.assertFalse(self.ref_exists(theirs))

    # ----- base rules and concurrency -----------------------------------

    def test_missing_base_with_existing_ref_is_rejected(self):
        first = self.create_work(
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        with self.assertRaises(repository.WorkHeadChangedError):
            self.create_work(
                base_commit_id=None,
                playground_id=self.playground_id,
                playground_commit_id=self.first_playground.commit_id,
                files={"work/main.py": b"new"},
            )
        ref = self.repository.work_ref(self.playground_id, self.student.id)
        self.assertEqual(
            self.git("rev-parse", ref).strip().decode("ascii"),
            first.commit_id,
        )

    def test_base_must_equal_current_head(self):
        first = self.create_work(
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        second = self.create_work(
            base_commit_id=first.commit_id,
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        # A stale base and a base that is not a Work commit are both rejected.
        for base in (first.commit_id, self.first_playground.commit_id, "a" * 40):
            with self.subTest(base=base):
                with self.assertRaises(repository.WorkHeadChangedError):
                    self.create_work(
                        base_commit_id=base,
                        playground_id=self.playground_id,
                        playground_commit_id=self.first_playground.commit_id,
                        files={"work/main.py": b"x"},
                    )
        ref = self.repository.work_ref(self.playground_id, self.student.id)
        self.assertEqual(
            self.git("rev-parse", ref).strip().decode("ascii"),
            second.commit_id,
        )

    def test_later_commit_must_keep_the_pinned_playground(self):
        first = self.create_work(
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        second = self.create_work(
            base_commit_id=first.commit_id,
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
            files={"work/main.py": b"updated"},
        )
        self.assertEqual(self.parents(second.commit_id), [first.commit_id])
        self.assertEqual(
            self.file_bytes(second.commit_id, ".picopg/work.json"),
            (
                '{"playgroundCommitId":"'
                + self.first_playground.commit_id
                + '"}'
            ).encode("utf-8"),
        )
        ref = self.repository.work_ref(self.playground_id, self.student.id)
        self.assertEqual(
            self.work_count(ref, self.first_playground.commit_id), b"2"
        )

    def test_later_commit_cannot_upgrade_the_pinned_playground(self):
        first = self.create_work(
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        newer = self.publish(self.teacher, self.playground_id, b"print(2)")
        self.assertNotEqual(
            newer.commit_id, self.first_playground.commit_id
        )
        # The student is authorized to read the newer Playground commit (the
        # Playground is public), but the pinned version on the existing Work
        # history is Server-owned and must not change.
        self.make_public(self.playground_id)

        ref = self.repository.work_ref(self.playground_id, self.student.id)
        with self.assertRaises(works.WorkValidationError) as caught:
            self.create_work(
                base_commit_id=first.commit_id,
                playground_id=self.playground_id,
                playground_commit_id=newer.commit_id,
                files={"work/main.py": b"upgraded"},
            )
        self.assertEqual(
            caught.exception.reason, works.REASON_WORK_PLAYGROUND_MISMATCH
        )
        self.assertIn(
            works.REASON_WORK_PLAYGROUND_MISMATCH, works.REASON_CODES
        )
        # No new commit was created and the ref did not move.
        self.assertEqual(
            self.git("rev-parse", ref).strip().decode("ascii"),
            first.commit_id,
        )
        self.assertEqual(
            self.work_count(ref, self.first_playground.commit_id), b"1"
        )

    def test_missing_base_work_manifest_fails_closed(self):
        commit = self.commit_tree({"work/main.py": b"x"})
        ref = self.repository.work_ref(self.playground_id, self.student.id)
        self.git("update-ref", ref, commit)

        with self.assertRaises(works.WorkValidationError) as caught:
            self.create_work(
                base_commit_id=commit,
                playground_id=self.playground_id,
                playground_commit_id=self.first_playground.commit_id,
                files={"work/main.py": b"later"},
            )
        self.assertEqual(
            caught.exception.reason, works.REASON_WORK_MANIFEST_INVALID
        )
        self.assertIn(
            works.REASON_WORK_MANIFEST_INVALID, works.REASON_CODES
        )
        self.assertEqual(
            self.git("rev-parse", ref).strip().decode("ascii"), commit
        )

    def test_malformed_base_work_manifest_fails_closed(self):
        ref = self.repository.work_ref(self.playground_id, self.student.id)
        for manifest in (
            b"{",
            b"null",
            b'{"playgroundCommitId":42}',
            b'{"other":"value"}',
        ):
            with self.subTest(manifest=manifest):
                commit = self.commit_tree(
                    {
                        "work/main.py": b"x",
                        ".picopg/work.json": manifest,
                    }
                )
                self.git("update-ref", ref, commit)
                with self.assertRaises(works.WorkValidationError) as caught:
                    self.create_work(
                        base_commit_id=commit,
                        playground_id=self.playground_id,
                        playground_commit_id=self.first_playground.commit_id,
                        files={"work/main.py": b"later"},
                    )
                self.assertEqual(
                    caught.exception.reason,
                    works.REASON_WORK_MANIFEST_INVALID,
                )
                self.assertEqual(
                    self.git("rev-parse", ref).strip().decode("ascii"),
                    commit,
                )

    def test_concurrent_ref_change_is_never_overwritten(self):
        first = self.create_work(
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        decoy = self.commit_tree(
            {"work/decoy.py": b"decoy"}, parent=self.first_playground.commit_id
        )
        ref = self.repository.work_ref(self.playground_id, self.student.id)

        original = repository._run_git
        moved = {"done": False}

        def racing(arguments, cwd=None, stdin=None, environment=None):
            completed = original(
                arguments, cwd=cwd, stdin=stdin, environment=environment
            )
            if not moved["done"] and arguments and arguments[0] == "commit-tree":
                moved["done"] = True
                self.git("update-ref", ref, decoy)
            return completed

        with mock.patch.object(repository, "_run_git", side_effect=racing):
            with self.assertRaises(repository.WorkHeadChangedError):
                self.create_work(
                    base_commit_id=first.commit_id,
                    playground_id=self.playground_id,
                    playground_commit_id=self.first_playground.commit_id,
                    files={"work/main.py": b"later"},
                )

        self.assertEqual(
            self.git("rev-parse", ref).strip().decode("ascii"), decoy
        )

    def test_first_creation_requires_missing_ref(self):
        # No prior ref: creation is the only valid shape.
        result = self.create_work(
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        self.assertTrue(self.ref_exists(
            self.repository.work_ref(self.playground_id, self.student.id)
        ))
        self.assertIsNone(result.base_commit_id)

    def test_current_work_head_tracks_the_owned_ref(self):
        self.assertIsNone(
            works.current_work_head(
                self.repository, self.playground_id, self.student.id
            )
        )
        result = self.create_work(
            playground_id=self.playground_id,
            playground_commit_id=self.first_playground.commit_id,
        )
        self.assertEqual(
            works.current_work_head(
                self.repository, self.playground_id, self.student.id
            ),
            result.commit_id,
        )
        # A different Student's absent ref stays ``None``.
        self.assertIsNone(
            works.current_work_head(
                self.repository, self.playground_id, self.other_student.id
            )
        )

    def test_invalid_work_ref_identifiers_never_reach_git(self):
        with mock.patch.object(repository, "_run_git") as spy:
            for playground_id in ("..", "a/b", "a b", ""):
                with self.subTest(playground_id=playground_id):
                    with self.assertRaises(
                        authorization.ContentAccessDeniedError
                    ):
                        works.create_work_commit(
                            self.connection,
                            self.student,
                            self.repository,
                            playground_id,
                            self.first_playground.commit_id,
                            self.work_files({"work/main.py": b"x"}),
                        )
            spy.assert_not_called()

    # ----- malformed playground trees -----------------------------------

    def test_crafted_playground_tree_with_work_path_is_rejected(self):
        self.make_public(self.playground_id)
        commit = self.commit_tree(
            {
                "picopg.json": MANIFEST_BYTES,
                "course/ok.py": b"ok",
                "work/smuggled.py": b"smuggled",
            }
        )
        # Point the Playground ref at the crafted commit so authorization
        # accepts it, then confirm the Work assembly refuses it.
        self.git(
            "update-ref",
            self.repository.playground_ref(self.playground_id),
            commit,
        )
        with self.assertRaises(works.WorkError) as caught:
            self.create_work(
                playground_id=self.playground_id,
                playground_commit_id=commit,
            )
        self.assertEqual(
            caught.exception.reason, works.REASON_PLAYGROUND_TREE_INVALID
        )

    def test_crafted_playground_tree_without_manifest_is_rejected(self):
        self.make_public(self.playground_id)
        commit = self.commit_tree({"course/ok.py": b"ok"})
        # Point the Playground ref at the crafted commit so authorization
        # accepts it, then confirm the Work assembly refuses a tree that is
        # missing the required ``picopg.json`` manifest.
        self.git(
            "update-ref",
            self.repository.playground_ref(self.playground_id),
            commit,
        )
        with self.assertRaises(works.WorkError) as caught:
            self.create_work(
                playground_id=self.playground_id,
                playground_commit_id=commit,
            )
        self.assertEqual(
            caught.exception.reason,
            works.REASON_PLAYGROUND_MISSING_MANIFEST,
        )
        self.assertIn(
            works.REASON_PLAYGROUND_MISSING_MANIFEST, works.REASON_CODES
        )
        ref = self.repository.work_ref(self.playground_id, self.student.id)
        self.assertFalse(self.ref_exists(ref))

    def test_playground_tree_without_manifest_reads_no_content(self):
        self.make_public(self.playground_id)
        commit = self.commit_tree({"starter/setup.py": b"setup"})

        def forbidden(*arguments, **kwargs):
            raise AssertionError("Playground content was read")

        with mock.patch.object(
            works.repository_module, "read_blobs", forbidden
        ):
            with self.assertRaises(works.WorkValidationError) as caught:
                works._read_playground_tree(self.repository, commit)
        self.assertEqual(
            caught.exception.reason,
            works.REASON_PLAYGROUND_MISSING_MANIFEST,
        )

    def test_valid_playground_tree_including_manifest_still_succeeds(self):
        self.make_public(self.playground_id)
        files = works._read_playground_tree(
            self.repository, self.first_playground.commit_id
        )
        self.assertIn("picopg.json", files)
        self.assertEqual(files["picopg.json"], MANIFEST_BYTES)
        self.assertEqual(files["course/module/app.py"], b"print(1)")

    def test_crafted_playground_tree_with_non_file_entry_is_rejected(self):
        self.make_public(self.playground_id)
        blob = (
            self.git("hash-object", "-w", "--stdin", input=MANIFEST_BYTES)
            .strip()
            .decode("ascii")
        )
        course_tree = self.git(
            "mktree",
            input=(
                f"160000 commit {self.first_playground.commit_id}\tsub\n"
            ).encode(),
        ).strip().decode("ascii")
        tree = self.git(
            "mktree",
            input=(
                f"100644 blob {blob}\tpicopg.json\n"
                f"040000 tree {course_tree}\tcourse\n"
            ).encode(),
        ).strip().decode("ascii")
        commit = (
            self.git(
                "commit-tree",
                tree,
                input=b"crafted\n",
                environment={
                    "GIT_AUTHOR_NAME": "Test Author",
                    "GIT_AUTHOR_EMAIL": "author@example.test",
                    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
                    "GIT_COMMITTER_NAME": "Test Author",
                    "GIT_COMMITTER_EMAIL": "author@example.test",
                    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
                },
            )
            .strip()
            .decode("ascii")
        )
        self.git(
            "update-ref",
            self.repository.playground_ref(self.playground_id),
            commit,
        )
        with self.assertRaises(works.WorkError) as caught:
            self.create_work(
                playground_id=self.playground_id,
                playground_commit_id=commit,
            )
        self.assertEqual(
            caught.exception.reason, works.REASON_PLAYGROUND_TREE_INVALID
        )


class WorkSyncDecodeTest(WorkTestBase):
    """Unit tests for the JSON Work sync decoder, independent of HTTP."""

    COMMIT = "a" * 40
    _DEFAULT_COMMIT = object()

    def decode(self, files, playground_commit_id=_DEFAULT_COMMIT, base=None):
        if playground_commit_id is self._DEFAULT_COMMIT:
            playground_commit_id = self.COMMIT
        payload = {
            "playgroundCommitId": playground_commit_id,
            "baseCommitId": base,
            "files": files,
        }
        return works.decode_work_sync_request(payload)

    @staticmethod
    def encoded(mapping):
        return [
            {
                "path": path,
                "data": base64.b64encode(data).decode("ascii"),
            }
            for path, data in mapping.items()
        ]

    def assert_reason(self, expected, files, **kwargs):
        with self.assertRaises(works.WorkValidationError) as caught:
            self.decode(files, **kwargs)
        self.assertEqual(caught.exception.reason, expected)

    def test_decodes_a_valid_request_to_a_bounded_mapping(self):
        request = self.decode(
            self.encoded(
                {"work/main.py": b"print(1)", "work/pkg/util.py": b"u"}
            ),
            base="b" * 40,
        )
        self.assertEqual(request.playground_commit_id, self.COMMIT)
        self.assertEqual(request.base_commit_id, "b" * 40)
        self.assertEqual(
            dict(request.files),
            {"work/main.py": b"print(1)", "work/pkg/util.py": b"u"},
        )
        # The decoded files are the same immutable type the domain expects.
        self.assertEqual(type(request.files), type(self.work_files({})))

    def test_empty_snapshot_is_allowed(self):
        request = self.decode([], base=None)
        self.assertEqual(dict(request.files), {})
        self.assertIsNone(request.base_commit_id)

    def test_payload_must_be_a_mapping_with_all_three_keys(self):
        for payload in ([], None, "x", 7):
            with self.subTest(payload=payload):
                with self.assertRaises(works.WorkValidationError) as caught:
                    works.decode_work_sync_request(payload)
                self.assertEqual(
                    caught.exception.reason, works.REASON_INVALID_PAYLOAD
                )

        for missing in ("playgroundCommitId", "baseCommitId", "files"):
            payload = {
                "playgroundCommitId": self.COMMIT,
                "baseCommitId": None,
                "files": [],
            }
            del payload[missing]
            with self.subTest(missing=missing):
                with self.assertRaises(works.WorkValidationError) as caught:
                    works.decode_work_sync_request(payload)
                self.assertEqual(
                    caught.exception.reason, works.REASON_INVALID_PAYLOAD
                )

    def test_commit_ids_must_be_non_empty_strings(self):
        for value in (None, "", 0, [], {}):
            with self.subTest(playground=value):
                self.assert_reason(
                    works.REASON_INVALID_PAYLOAD,
                    [],
                    playground_commit_id=value,
                )
        for value in ("", 0, [], {}):
            with self.subTest(base=value):
                self.assert_reason(
                    works.REASON_INVALID_PAYLOAD, [], base=value
                )

    def test_files_must_be_a_list(self):
        for value in (None, {}, "x", 0):
            with self.subTest(files=value):
                self.assert_reason(works.REASON_INVALID_FILES, value)

    def test_entry_must_be_a_mapping_with_string_data(self):
        self.assert_reason(works.REASON_INVALID_ENTRY, ["not-an-entry"])
        self.assert_reason(
            works.REASON_ENTRY_DATA_NOT_BYTES, [{"path": "work/a.py"}]
        )
        self.assert_reason(
            works.REASON_ENTRY_DATA_NOT_BYTES,
            [{"path": "work/a.py", "data": b"raw"}],
        )

    def test_malformed_base64_is_rejected(self):
        for encoded in ("!!!!", "abc", "A" * 5, "x", "AB=C", "\u00e9"):
            with self.subTest(encoded=encoded):
                self.assert_reason(
                    works.REASON_INVALID_BASE64,
                    [{"path": "work/a.py", "data": encoded}],
                )

    def test_duplicate_paths_are_rejected(self):
        self.assert_reason(
            works.REASON_DUPLICATE_PATH,
            [
                {"path": "work/a.py", "data": "MQ=="},
                {"path": "work/a.py", "data": "Mg=="},
            ],
        )

        # Two distinct raw spellings that normalize to one NFC path collide.
        nfc = "work/caf\u00e9.py"
        nfd = "work/cafe\u0301.py"
        self.assertNotEqual(nfc, nfd)
        self.assert_reason(
            works.REASON_DUPLICATE_PATH,
            [
                {"path": nfc, "data": "MQ=="},
                {"path": nfd, "data": "Mg=="},
            ],
        )

    def test_path_rules_reuse_the_work_validator(self):
        cases = [
            ("course/module/app.py", works.REASON_PATH_NOT_ALLOWED),
            (".picopg/work.json", works.REASON_PATH_NOT_ALLOWED),
            ("work/../escape.py", works.REASON_PATH_DOT_SEGMENT),
            ("work//empty.py", works.REASON_PATH_EMPTY_SEGMENT),
            ("work/a\x00b.py", works.REASON_PATH_NUL),
        ]
        for path, reason in cases:
            with self.subTest(path=path):
                self.assert_reason(
                    reason, [{"path": path, "data": "MQ=="}]
                )

    def test_entry_count_is_checked_before_decoding(self):
        with mock.patch.object(works, "MAX_WORK_ENTRY_COUNT", 1):
            # The second entry has malformed base64, so this only passes if
            # the count is rejected before any entry is decoded.
            self.assert_reason(
                works.REASON_TOO_MANY_ENTRIES,
                [
                    {"path": "work/a.py", "data": "MQ=="},
                    {"path": "work/b.py", "data": "!!!!"},
                ],
            )

    def test_file_and_total_limits_are_enforced(self):
        with mock.patch.object(works, "MAX_WORK_FILE_BYTES", 2):
            self.assert_reason(
                works.REASON_FILE_TOO_LARGE,
                [{"path": "work/a.py", "data": "YWJj"}],
            )

        with mock.patch.object(works, "MAX_WORK_TOTAL_BYTES", 2):
            self.assert_reason(
                works.REASON_WORK_TOO_LARGE,
                [
                    {"path": "work/a.py", "data": "YWI="},
                    {"path": "work/b.py", "data": "Y2Q="},
                ],
            )

    def test_sync_body_ceiling_covers_the_maximum_snapshot(self):
        self.assertEqual(
            works.MAX_SYNC_JSON_BODY_BYTES, 2 * works.MAX_WORK_TOTAL_BYTES
        )
        # Base64 of the largest allowed snapshot must fit inside the ceiling.
        base64_size = 4 * ((works.MAX_WORK_TOTAL_BYTES + 2) // 3)
        self.assertLess(base64_size, works.MAX_SYNC_JSON_BODY_BYTES)

    def test_new_reason_codes_are_registered(self):
        for reason in (
            works.REASON_INVALID_PAYLOAD,
            works.REASON_INVALID_FILES,
            works.REASON_INVALID_BASE64,
        ):
            with self.subTest(reason=reason):
                self.assertIn(reason, works.REASON_CODES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
