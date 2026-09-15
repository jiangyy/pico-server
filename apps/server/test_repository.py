"""Tests for private bare Git repository bootstrap."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Allow ``import repository`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from repository import (  # noqa: E402
    DEFAULT_REPOSITORY_PATH,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
