"""Tests for private bare Git repository bootstrap."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock

# Allow ``import repository`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bundles  # noqa: E402
import repository  # noqa: E402
from repository import (  # noqa: E402
    DEFAULT_REPOSITORY_PATH,
    CommitMetadata,
    PlaygroundHeadChangedError,
    PublishResult,
    Repository,
    RepositoryError,
    _check_ref_format,
    open_repository,
)


class RepositoryBootstrapTest(unittest.TestCase):
    """Filesystem-level tests using temporary repositories."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "picopg.git"

    def test_default_path(self):
        self.assertEqual(DEFAULT_REPOSITORY_PATH, Path("data/picopg.git"))

    def test_fresh_init_creates_bare_repository(self):
        repository = open_repository(self.path)
        self.assertIsInstance(repository, Repository)
        self.assertEqual(repository.path, self.path)
        self.assertTrue(self.path.is_dir())
        self.assertTrue((self.path / "HEAD").is_file())
        self.assertTrue((self.path / "objects").is_dir())
        self.assertTrue((self.path / "refs").is_dir())

    def test_repository_is_bare_and_has_no_worktree(self):
        open_repository(self.path)

        result = subprocess.run(
            ["git", "-C", str(self.path), "rev-parse", "--is-bare-repository"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "true")

        worktree = subprocess.run(
            ["git", "-C", str(self.path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(worktree.returncode, 0)

        # A bare repository has no checked-out worktree or index file.
        self.assertFalse((self.path / ".git").exists())
        self.assertFalse((self.path / "index").exists())

    def test_reopen_is_idempotent(self):
        first = open_repository(self.path)
        second = open_repository(self.path)
        self.assertEqual(first.path, second.path)
        self.assertTrue((self.path / "HEAD").is_file())

    def test_existing_plain_directory_is_rejected(self):
        self.path.mkdir()
        with self.assertRaises(RepositoryError):
            open_repository(self.path)

    def test_existing_non_bare_repository_is_rejected(self):
        subprocess.run(
            ["git", "init", str(self.path)],
            capture_output=True,
            check=True,
        )
        with self.assertRaises(RepositoryError):
            open_repository(self.path)

    def test_existing_file_is_rejected(self):
        self.path.write_text("not a repository", encoding="utf-8")
        with self.assertRaises(RepositoryError):
            open_repository(self.path)

    def test_errors_do_not_leak_git_output(self):
        self.path.mkdir()
        with self.assertRaises(RepositoryError) as context:
            open_repository(self.path)
        self.assertNotIn("fatal", str(context.exception).lower())


class RefNamespaceTest(unittest.TestCase):
    """Ref helper tests; the repository does not need to exist on disk."""

    def setUp(self):
        self.repository = Repository(Path("/nonexistent/picopg.git"))

    def test_playground_ref(self):
        self.assertEqual(
            self.repository.playground_ref("pg-abc123"),
            "refs/heads/playgrounds/pg-abc123",
        )

    def test_work_ref(self):
        self.assertEqual(
            self.repository.work_ref("pg-abc123", "student-42"),
            "refs/heads/works/pg-abc123/student-42",
        )

    def test_only_namespaced_helpers_are_public(self):
        public = {
            name for name in dir(self.repository) if not name.startswith("_")
        }
        self.assertEqual(public, {"path", "playground_ref", "work_ref"})

    def test_unsafe_identifiers_are_rejected(self):
        unsafe = [
            "",
            ".",
            "..",
            "a/b",
            "a\\b",
            "a b",
            "a~b",
            "a^b",
            "a:b",
            "a?b",
            "a*b",
            "a[b",
            "a..b",
            "a@{b",
            ".hidden",
            "trail.",
            "name.lock",
            "line\nbreak",
            "\x7f",
        ]
        for identifier in unsafe:
            with self.subTest(identifier=identifier):
                with self.assertRaises(RepositoryError):
                    self.repository.playground_ref(identifier)
                with self.assertRaises(RepositoryError):
                    self.repository.work_ref(identifier, "student-42")
                with self.assertRaises(RepositoryError):
                    self.repository.work_ref("pg-abc123", identifier)

    def test_non_string_identifiers_are_rejected(self):
        for identifier in (None, 1, b"bytes", ["list"]):
            with self.subTest(identifier=identifier):
                with self.assertRaises(RepositoryError):
                    self.repository.playground_ref(identifier)

    def test_git_ref_validation_guard(self):
        _check_ref_format("refs/heads/playgrounds/valid-id")
        with self.assertRaises(RepositoryError):
            _check_ref_format("refs/heads/playgrounds/..")


class PlaygroundPublishingTest(unittest.TestCase):
    """End-to-end tests of bundle publishing through Git plumbing."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "picopg.git"
        self.repository = open_repository(self.path)

    # -- helpers ---------------------------------------------------------

    def make_bundle(self, entries):
        return bundles.validate_bundle(
            [bundles.BundleEntry(path, data) for path, data in entries]
        )

    def metadata(self, message="publish playground"):
        return CommitMetadata(
            name="Test Author",
            email="author@example.test",
            timestamp="2026-01-01T00:00:00+00:00",
            message=message,
        )

    def default_entries(self, course_data=b"print(1)"):
        return [
            (
                "picopg.json",
                b'{"schemaVersion":1,"runtime":"python"}',
            ),
            ("course/module/app.py", course_data),
            ("starter/setup.py", b"setup"),
        ]

    def git(self, *arguments, input=None, check=True):
        return subprocess.run(
            ["git", "-C", str(self.path), *arguments],
            input=input,
            capture_output=True,
            check=check,
        )

    def commit_tree(self, tree_id, message=b"decoy\n"):
        environment = dict(os.environ)
        environment.update(
            {
                "GIT_AUTHOR_NAME": "Decoy",
                "GIT_AUTHOR_EMAIL": "decoy@example.test",
                "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
                "GIT_COMMITTER_NAME": "Decoy",
                "GIT_COMMITTER_EMAIL": "decoy@example.test",
                "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
            }
        )
        completed = subprocess.run(
            ["git", "-C", str(self.path), "commit-tree", tree_id],
            input=message,
            capture_output=True,
            check=True,
            env=environment,
        )
        return completed.stdout.strip().decode("ascii")

    def ref_exists(self, ref):
        completed = subprocess.run(
            ["git", "-C", str(self.path), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
        )
        return completed.returncode == 0

    # -- publishing behaviour -------------------------------------------

    def test_first_publish_creates_single_root_commit(self):
        playground_id = "pg-first"
        result = repository.publish_playground(
            self.repository,
            playground_id,
            self.make_bundle(self.default_entries()),
            self.metadata(),
        )

        self.assertIsInstance(result, PublishResult)
        self.assertEqual(result.playground_id, playground_id)
        self.assertFalse(result.no_op)

        ref = self.repository.playground_ref(playground_id)
        self.assertEqual(result.commit_id, self.git(
            "rev-parse", ref
        ).stdout.strip().decode("ascii"))

        # Plumbing: exactly one commit and it has no parents.
        lineage = self.git(
            "rev-list", "--parents", "-n", "1", result.commit_id
        ).stdout.split()
        self.assertEqual(len(lineage), 1)
        self.assertEqual(
            self.git("rev-list", "--count", ref).stdout.strip(), b"1"
        )

    def test_second_publish_is_linear_with_exactly_one_parent(self):
        playground_id = "pg-linear"
        first = repository.publish_playground(
            self.repository,
            playground_id,
            self.make_bundle(self.default_entries(b"print(1)")),
            self.metadata(),
        )
        second = repository.publish_playground(
            self.repository,
            playground_id,
            self.make_bundle(self.default_entries(b"print(2)")),
            self.metadata(),
        )

        self.assertFalse(second.no_op)
        lineage = self.git(
            "rev-list", "--parents", "-n", "1", second.commit_id
        ).stdout.split()
        self.assertEqual(len(lineage), 2)
        self.assertEqual(lineage[1].decode("ascii"), first.commit_id)

        ref = self.repository.playground_ref(playground_id)
        self.assertEqual(
            self.git("rev-parse", ref).stdout.strip().decode("ascii"),
            second.commit_id,
        )
        self.assertEqual(
            self.git("rev-list", "--count", ref).stdout.strip(), b"2"
        )

    def test_equal_tree_is_a_no_op(self):
        playground_id = "pg-noop"
        first = repository.publish_playground(
            self.repository,
            playground_id,
            self.make_bundle(self.default_entries()),
            self.metadata(),
        )

        observed = []
        original = repository._run_git

        def spy(arguments, cwd=None, stdin=None, environment=None):
            observed.append(arguments[0] if arguments else "")
            return original(
                arguments, cwd=cwd, stdin=stdin, environment=environment
            )

        with mock.patch.object(repository, "_run_git", side_effect=spy):
            replay = repository.publish_playground(
                self.repository,
                playground_id,
                self.make_bundle(self.default_entries()),
                self.metadata(),
            )

        self.assertTrue(replay.no_op)
        self.assertEqual(replay.commit_id, first.commit_id)
        self.assertNotIn("commit-tree", observed)
        self.assertNotIn("update-ref", observed)

        ref = self.repository.playground_ref(playground_id)
        self.assertEqual(
            self.git("rev-parse", ref).stdout.strip().decode("ascii"),
            first.commit_id,
        )
        self.assertEqual(
            self.git("rev-list", "--count", ref).stdout.strip(), b"1"
        )

    def test_concurrent_ref_change_raises_head_changed(self):
        playground_id = "pg-race"
        first = repository.publish_playground(
            self.repository,
            playground_id,
            self.make_bundle(self.default_entries(b"print(1)")),
            self.metadata(),
        )
        ref = self.repository.playground_ref(playground_id)
        head_tree = self.git(
            "rev-parse", f"{first.commit_id}^{{tree}}"
        ).stdout.strip().decode("ascii")
        decoy = self.commit_tree(head_tree)

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
            with self.assertRaises(PlaygroundHeadChangedError) as context:
                repository.publish_playground(
                    self.repository,
                    playground_id,
                    self.make_bundle(self.default_entries(b"print(2)")),
                    self.metadata(),
                )

        self.assertEqual(context.exception.reason, "playground_head_changed")
        # The concurrent ref target is preserved, never overwritten.
        self.assertEqual(
            self.git("rev-parse", ref).stdout.strip().decode("ascii"), decoy
        )

    def test_tree_contains_only_the_allowlisted_paths(self):
        result = repository.publish_playground(
            self.repository,
            "pg-tree",
            self.make_bundle(self.default_entries()),
            self.metadata(),
        )
        paths = self.git(
            "ls-tree", "-r", "--name-only", result.commit_id
        ).stdout.decode("utf-8").splitlines()

        self.assertIn("course/module/app.py", paths)
        self.assertIn("starter/setup.py", paths)
        self.assertIn("picopg.json", paths)
        for path in paths:
            self.assertTrue(
                path == "picopg.json"
                or path.startswith("course/")
                or path.startswith("starter/"),
                path,
            )

    def test_unsafe_playground_ids_never_reach_git(self):
        unsafe = ["", None, 7, "a/b", "..", "a b", "a..b", ".hidden"]
        with mock.patch.object(repository, "_run_git") as spy:
            for playground_id in unsafe:
                with self.subTest(playground_id=playground_id):
                    with self.assertRaises(RepositoryError):
                        repository.publish_playground(
                            self.repository,
                            playground_id,
                            self.make_bundle(self.default_entries()),
                            self.metadata(),
                        )
            spy.assert_not_called()

    def test_commit_metadata_accepts_date_alias(self):
        by_timestamp = CommitMetadata(
            name="n",
            email="e@x",
            timestamp="2026-01-01T00:00:00+00:00",
            message="m",
        )
        by_date = CommitMetadata(
            name="n",
            email="e@x",
            date="2026-01-01T00:00:00+00:00",
            message="m",
        )
        self.assertEqual(by_date, by_timestamp)
        self.assertEqual(by_date.date, by_date.timestamp)
        with self.assertRaises(TypeError):
            CommitMetadata(
                name="n",
                email="e@x",
                timestamp="a",
                date="b",
                message="m",
            )

    def test_metadata_is_validated_before_any_object_write(self):
        valid_bundle = self.make_bundle(self.default_entries())
        bad_metadata = [
            None,
            "metadata",
            CommitMetadata("", "a@b", "2026-01-01T00:00:00+00:00", "m"),
            CommitMetadata("n", "", "2026-01-01T00:00:00+00:00", "m"),
            CommitMetadata("n", "a@b", "", "m"),
            CommitMetadata("n", "a@b", "2026-01-01T00:00:00+00:00", ""),
            CommitMetadata("bad\nname", "a@b", "2026-01-01T00:00:00+00:00", "m"),
            CommitMetadata("n", "bad\x00@b", "2026-01-01T00:00:00+00:00", "m"),
            CommitMetadata("n", "a@b", "date\n", "m"),
            CommitMetadata("n", "a@b", "2026-01-01T00:00:00+00:00", "m\n"),
            CommitMetadata(1, "a@b", "2026-01-01T00:00:00+00:00", "m"),
        ]

        with mock.patch.object(repository, "_run_git") as spy:
            for metadata in bad_metadata:
                with self.subTest(metadata=metadata):
                    with self.assertRaises(RepositoryError):
                        repository.publish_playground(
                            self.repository, "pg-meta", valid_bundle, metadata
                        )
            spy.assert_not_called()

        ref = self.repository.playground_ref("pg-meta")
        self.assertFalse(self.ref_exists(ref))

    def test_bundle_must_be_the_normalized_read_only_mapping(self):
        valid = self.make_bundle(self.default_entries())
        metadata = self.metadata()
        rejected = [
            None,
            [],
            valid.copy() if hasattr(valid, "copy") else {},
            {path: data for path, data in self.default_entries()},
            MappingProxyType({1: b"x"}),
            MappingProxyType({}),
            MappingProxyType({"picopg.json": "text"}),
            MappingProxyType({"picopg.json": bytearray(b"x")}),
            MappingProxyType({"work/secret": b"x"}),
            MappingProxyType({"course/../escape": b"x"}),
            MappingProxyType({"course/": b"x"}),
            MappingProxyType({"/absolute": b"x"}),
            MappingProxyType({"a\\b": b"x"}),
        ]

        for bundle in rejected:
            with self.subTest(bundle=bundle):
                with self.assertRaises(RepositoryError):
                    repository.publish_playground(
                        self.repository, "pg-bundle", bundle, metadata
                    )

        # A stale but well-typed mapping is still rejected before any write.
        conflicting = MappingProxyType(
            {
                "picopg.json": b'{"schemaVersion":1,"runtime":"python"}',
                "course/a": b"file",
                "course/a/b": b"nested",
            }
        )
        with self.assertRaises(RepositoryError):
            repository.publish_playground(
                self.repository, "pg-bundle", conflicting, metadata
            )
        self.assertFalse(self.ref_exists(self.repository.playground_ref("pg-bundle")))

    def test_nested_and_unusual_legal_filenames(self):
        entries = [
            (
                "picopg.json",
                b'{"schemaVersion":1,"runtime":"python"}',
            ),
            ("course/a b/c-d.e/app.py", b"print(1)"),
            ("course/deep/one/two/three.txt", b"deep"),
            ("starter/nai\u0308ve/setup file.py", b"setup"),
            ("starter/[brackets]&plus+name.txt", b"starter"),
        ]
        result = repository.publish_playground(
            self.repository,
            "pg-unusual",
            self.make_bundle(entries),
            self.metadata(),
        )
        raw = self.git(
            "ls-tree", "-r", "-z", "--name-only", result.commit_id
        ).stdout
        paths = [name.decode("utf-8") for name in raw.split(b"\x00") if name]
        self.assertIn("course/a b/c-d.e/app.py", paths)
        self.assertIn("course/deep/one/two/three.txt", paths)
        # validate_bundle normalizes to NFC, so the decomposed form collapses.
        self.assertIn("starter/na\u00efve/setup file.py", paths)
        self.assertIn("starter/[brackets]&plus+name.txt", paths)

    def test_argv_has_no_local_path_or_arbitrary_ref(self):
        seen = []
        original = repository._run_git

        def spy(arguments, cwd=None, stdin=None, environment=None):
            seen.append(tuple(arguments))
            return original(
                arguments, cwd=cwd, stdin=stdin, environment=environment
            )

        with mock.patch.object(repository, "_run_git", side_effect=spy):
            repository.publish_playground(
                self.repository,
                "pg-argv",
                self.make_bundle(self.default_entries()),
                self.metadata(),
            )

        expected_ref = self.repository.playground_ref("pg-argv")
        for arguments in seen:
            for argument in arguments:
                self.assertNotIn(str(self.path), argument)
                self.assertNotIn("course/", argument)
                self.assertNotIn("starter/", argument)
                if "refs/" in argument:
                    self.assertEqual(argument, expected_ref)

    def test_environment_redirection_is_sanitized(self):
        decoy_path = Path(self.temporary_directory.name) / "decoy.git"
        open_repository(decoy_path)
        playground_id = "pg-env"
        ref = self.repository.playground_ref(playground_id)

        redirected = {
            "GIT_DIR": str(decoy_path),
            "GIT_WORK_TREE": str(decoy_path),
            "GIT_OBJECT_DIRECTORY": str(decoy_path / "objects"),
            "GIT_AUTHOR_NAME": "Attacker",
            "GIT_AUTHOR_EMAIL": "attacker@example.test",
            "GIT_COMMITTER_NAME": "Attacker",
            "GIT_COMMITTER_EMAIL": "attacker@example.test",
        }
        with mock.patch.dict(os.environ, redirected):
            result = repository.publish_playground(
                self.repository,
                playground_id,
                self.make_bundle(self.default_entries()),
                self.metadata(),
            )

        # The target repository received the ref, not the decoy.
        self.assertTrue(self.ref_exists(ref))
        decoy_check = subprocess.run(
            ["git", "-C", str(decoy_path), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
        )
        self.assertNotEqual(decoy_check.returncode, 0)

        author = self.git(
            "log", "--format=%an <%ae>", "-n", "1", result.commit_id
        ).stdout.decode("utf-8").strip()
        self.assertEqual(author, "Test Author <author@example.test>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
