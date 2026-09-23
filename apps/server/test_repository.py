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

    def test_the_three_ref_shapes(self):
        """两个命名空间加一个只读的旧命名空间，各自负责一件事。"""

        # history：稳定的可达性根
        self.assertEqual(
            self.repository.history_ref("pg-abc123"),
            "refs/picopg/history/pg-abc123",
        )
        # pending：一次 Publish 的临时根
        self.assertEqual(
            self.repository.pending_ref("deadbeef"),
            "refs/picopg/pending/deadbeef",
        )
        # legacy：迁移时才读，新代码不写
        self.assertEqual(
            self.repository.legacy_ref("pg-abc123"),
            "refs/heads/playgrounds/pg-abc123",
        )

    def test_only_namespaced_helpers_are_public(self):
        public = {
            name for name in dir(self.repository) if not name.startswith("_")
        }
        self.assertEqual(
            public,
            {"path", "history_ref", "assignment_ref", "pending_ref", "legacy_ref"},
        )

    def test_a_pending_ref_rejects_an_id_that_is_not_one_segment(self):
        for hostile in ("", "a/b", "a\\b", ".hidden", None, 7):
            with self.subTest(hostile=hostile):
                with self.assertRaises(repository.RepositoryError):
                    self.repository.pending_ref(hostile)

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
                    self.repository.history_ref(identifier)

    def test_non_string_identifiers_are_rejected(self):
        for identifier in (None, 1, b"bytes", ["list"]):
            with self.subTest(identifier=identifier):
                with self.assertRaises(RepositoryError):
                    self.repository.history_ref(identifier)

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

        ref = self.repository.history_ref(playground_id)
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

        ref = self.repository.history_ref(playground_id)
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

        ref = self.repository.history_ref(playground_id)
        self.assertEqual(
            self.git("rev-parse", ref).stdout.strip().decode("ascii"),
            first.commit_id,
        )
        self.assertEqual(
            self.git("rev-list", "--count", ref).stdout.strip(), b"1"
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

        ref = self.repository.history_ref("pg-meta")
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
        self.assertFalse(self.ref_exists(self.repository.history_ref("pg-bundle")))

    def test_forged_mapping_proxy_is_revalidated_before_any_write(self):
        metadata = self.metadata()
        manifest = b'{"schemaVersion":1,"runtime":"python"}'
        forged = {
            "invalid json": MappingProxyType(
                {"picopg.json": b"{", "course/a.py": b"x"}
            ),
            "schema 99": MappingProxyType(
                {
                    "picopg.json": b'{"schemaVersion":99,"runtime":"python"}',
                    "course/a.py": b"x",
                }
            ),
            "runtime python3": MappingProxyType(
                {
                    "picopg.json": b'{"schemaVersion":1,"runtime":"python3"}',
                    "course/a.py": b"x",
                }
            ),
            "duplicate normalized path": MappingProxyType(
                {
                    "picopg.json": manifest,
                    "course/caf\u00e9.py": b"1",
                    "course/cafe\u0301.py": b"2",
                }
            ),
            "control character": MappingProxyType(
                {"picopg.json": manifest, "course/a\x01.py": b"x"}
            ),
            "lone surrogate": MappingProxyType(
                {"picopg.json": manifest, "course/\ud800.py": b"x"}
            ),
            "file directory collision": MappingProxyType(
                {
                    "picopg.json": manifest,
                    "course/a": b"x",
                    "course/a/b.py": b"y",
                }
            ),
        }

        for label, bundle in forged.items():
            with self.subTest(label=label):
                with mock.patch.object(
                    repository, "_write_bundle_tree"
                ) as writer:
                    with self.assertRaises(RepositoryError) as ctx:
                        repository.publish_playground(
                            self.repository, "pg-forged", bundle, metadata
                        )
                writer.assert_not_called()
                self.assertIsNone(ctx.exception.__cause__)
                self.assertIsNone(ctx.exception.__context__)
                self.assertNotIn("course/", str(ctx.exception))
        self.assertFalse(
            self.ref_exists(self.repository.history_ref("pg-forged"))
        )

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

        # 出现的 ref 只能是我们自己的两个命名空间里的，而且是这次操作该用的那个：
        # 调用方的字符串永远不直接成为 ref 名。
        history = self.repository.history_ref("pg-argv")
        pending = {
            f"refs/picopg/pending/{publish_id}" for publish_id in seen_publish_ids(seen)
        }
        allowed = {history, *pending}
        for arguments in seen:
            for argument in arguments:
                self.assertNotIn(str(self.path), argument)
                self.assertNotIn("course/", argument)
                self.assertNotIn("starter/", argument)
                if "refs/" in argument:
                    self.assertIn(argument, allowed, f"出现了不在命名空间里的 ref: {argument}")

    def test_invalid_playground_ids_never_start_git(self):
        seen = []
        original = repository._run_git

        def spy(arguments, cwd=None, stdin=None, environment=None):
            seen.append(tuple(arguments))
            return original(
                arguments, cwd=cwd, stdin=stdin, environment=environment
            )

        invalid = (
            "",
            "../escape",
            "bad/id",
            "bad\\id",
            "bad id",
            "bad%2fid",
            ".leading",
            "trailing.",
            "double..dot",
            "name.lock",
            "NAME.LOCK",
            "caf\u00e9",
            "has\x01control",
            "a" * 129,
            None,
            7,
        )
        with mock.patch.object(repository, "_run_git", spy):
            for playground_id in invalid:
                with self.subTest(playground_id=playground_id):
                    with self.assertRaises(RepositoryError):
                        self.repository.history_ref(playground_id)
        self.assertEqual(seen, [])

        self.assertFalse(repository.valid_playground_id("a" * 129))
        self.assertFalse(repository.valid_playground_id("a..b"))
        self.assertFalse(repository.valid_playground_id("bad/id"))
        self.assertTrue(repository.valid_playground_id("a"))
        self.assertTrue(repository.valid_playground_id("a" * 128))
        self.assertTrue(repository.valid_playground_id("A-z.0_9"))

    def test_environment_redirection_is_sanitized(self):
        decoy_path = Path(self.temporary_directory.name) / "decoy.git"
        open_repository(decoy_path)
        playground_id = "pg-env"
        ref = self.repository.history_ref(playground_id)

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

    def test_inherited_git_control_variables_cannot_redirect_publish(self):
        decoy_path = Path(self.temporary_directory.name) / "decoy.git"
        open_repository(decoy_path)
        playground_id = "pg-control"
        ref = self.repository.history_ref(playground_id)
        trace_file = Path(self.temporary_directory.name) / "git-trace.log"

        redirected = {
            "GIT_COMMON_DIR": str(decoy_path),
            "GIT_OBJECT_DIRECTORY": str(decoy_path / "objects"),
            "GIT_CONFIG_PARAMETERS": "'core.sharedRepository=group'",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.bare",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_TRACE": str(trace_file),
            "GIT_TRACE2_EVENT": str(trace_file),
            "GIT_TRACE_PACK_ACCESS": str(trace_file),
        }
        with mock.patch.dict(os.environ, redirected):
            result = repository.publish_playground(
                self.repository,
                playground_id,
                self.make_bundle(self.default_entries()),
                self.metadata(),
            )

        self.assertEqual(
            self.git("rev-parse", ref).stdout.strip().decode("ascii"),
            result.commit_id,
        )
        decoy_check = subprocess.run(
            [
                "git",
                "-C",
                str(decoy_path),
                "rev-parse",
                "--verify",
                "--quiet",
                ref,
            ],
            capture_output=True,
        )
        self.assertNotEqual(decoy_check.returncode, 0)
        fsck = subprocess.run(
            ["git", "-C", str(self.path), "fsck", "--no-progress"],
            capture_output=True,
        )
        self.assertEqual(
            fsck.returncode, 0, fsck.stderr.decode("utf-8", "replace")
        )
        self.assertFalse(trace_file.exists())

    def test_reference_transaction_hook_cannot_be_injected(self):
        hooks_path = Path(self.temporary_directory.name) / "hooks"
        hooks_path.mkdir()
        marker = Path(self.temporary_directory.name) / "hook-ran"
        hook = hooks_path / "reference-transaction"
        hook.write_text(
            "#!/bin/sh\n"
            f"echo ran >> '{marker}'\n"
            "cat >/dev/null\n"
            "exit 0\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)

        injected = {
            "GIT_CONFIG_PARAMETERS": f"'core.hooksPath={hooks_path}'"
        }
        with mock.patch.dict(os.environ, injected):
            repository.publish_playground(
                self.repository,
                "pg-hook",
                self.make_bundle(self.default_entries()),
                self.metadata(),
            )

        self.assertFalse(marker.exists())

    def test_a_failing_ref_update_is_a_repository_error(self):
        # 一个写 ref 失败的 Git 调用不能被当成"有人抢先改了"：它是磁盘/锁/仓库
        # 故障，必须原样冒出来，而不是伪装成并发。
        playground_id = "pg-lock-failure"
        first = repository.publish_playground(
            self.repository,
            playground_id,
            self.make_bundle(self.default_entries(b"print(1)")),
            self.metadata(),
        )
        ref = self.repository.history_ref(playground_id)
        original = repository._run_git

        def failing_update_ref(arguments, cwd=None, stdin=None, environment=None):
            if arguments and arguments[0] == "update-ref":
                return subprocess.CompletedProcess(
                    ["git", *arguments], 1, b"", b"fatal: cannot lock ref"
                )
            return original(arguments, cwd=cwd, stdin=stdin, environment=environment)

        with mock.patch.object(repository, "_run_git", side_effect=failing_update_ref):
            with self.assertRaises(RepositoryError):
                repository.converge_history_ref(
                    self.repository, playground_id, first.commit_id
                )

        # 失败没有动过 ref
        self.assertEqual(
            self.git("rev-parse", ref).stdout.strip().decode("ascii"),
            first.commit_id,
        )


def seen_publish_ids(seen):
    """从 git argv 里取出所有 pending ref 用的 event id。"""

    ids = set()
    for arguments in seen:
        for argument in arguments:
            if argument.startswith("refs/picopg/pending/"):
                ids.add(argument[len("refs/picopg/pending/"):])
    return ids


class CommitTreeReadTest(unittest.TestCase):
    """Read-only commit tree and blob access without any checkout."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "picopg.git"
        self.repository = open_repository(self.path)

    def make_bundle(self, entries):
        return bundles.validate_bundle(
            [bundles.BundleEntry(path, data) for path, data in entries]
        )

    def default_entries(self, course_data=b"print(1)"):
        return [
            ("picopg.json", b'{"schemaVersion":1,"runtime":"python"}'),
            ("course/module/app.py", course_data),
            ("starter/setup.py", b"setup"),
        ]

    def metadata(self):
        return CommitMetadata(
            name="Test Author",
            email="author@example.test",
            timestamp="2026-01-01T00:00:00+00:00",
            message="publish playground",
        )

    def git(self, *arguments, input=None, check=True):
        return subprocess.run(
            ["git", "-C", str(self.path), *arguments],
            input=input,
            capture_output=True,
            check=check,
        )

    def commit_tree(self, tree_id, message=b"crafted\n"):
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

    def publish(self, course_data=b"print(1)"):
        return repository.publish_playground(
            self.repository,
            "pg-read",
            self.make_bundle(self.default_entries(course_data)),
            self.metadata(),
        )

    def test_list_commit_tree_returns_recursive_blob_entries(self):
        result = self.publish()
        entries = repository.list_commit_tree(
            self.repository, result.commit_id
        )
        by_path = {entry.path: entry for entry in entries}
        self.assertEqual(
            set(by_path),
            {"picopg.json", "course/module/app.py", "starter/setup.py"},
        )
        for path, entry in by_path.items():
            with self.subTest(path=path):
                self.assertEqual(entry.kind, "blob")
                self.assertEqual(entry.mode, "100644")
                self.assertIsNotNone(entry.size)
                self.assertEqual(
                    entry.object_id,
                    self.git(
                        "rev-parse", f"{result.commit_id}:{path}"
                    ).stdout.strip().decode("ascii"),
                )

    def test_list_commit_tree_rejects_symbolic_and_short_ids(self):
        result = self.publish()
        for commit_id in (
            "HEAD",
            result.commit_id[:12],
            result.commit_id.upper(),
            "refs/heads/playgrounds/pg-read",
            None,
            7,
        ):
            with self.subTest(commit_id=commit_id):
                with self.assertRaises(RepositoryError):
                    repository.list_commit_tree(self.repository, commit_id)

    def test_list_commit_tree_rejects_missing_commit(self):
        self.publish()
        with self.assertRaises(RepositoryError):
            repository.list_commit_tree(self.repository, "a" * 40)

    def test_resolve_commit_requires_an_exact_existing_commit(self):
        result = self.publish()
        self.assertEqual(
            repository.resolve_commit(self.repository, result.commit_id),
            result.commit_id,
        )
        self.assertIsNone(
            repository.resolve_commit(self.repository, "a" * 40)
        )
        tree_id = (
            self.git("rev-parse", f"{result.commit_id}^{{tree}}")
            .stdout.strip()
            .decode("ascii")
        )
        self.assertIsNone(
            repository.resolve_commit(self.repository, tree_id)
        )
        with self.assertRaises(RepositoryError):
            repository.resolve_commit(self.repository, "HEAD")

    def test_read_blobs_returns_exact_bytes(self):
        binary = bytes(range(256))
        entries = [
            ("picopg.json", b'{"schemaVersion":1,"runtime":"python"}'),
            ("course/data.bin", binary),
            ("course/empty.txt", b""),
        ]
        result = repository.publish_playground(
            self.repository,
            "pg-read",
            self.make_bundle(entries),
            self.metadata(),
        )
        listed = repository.list_commit_tree(
            self.repository, result.commit_id
        )
        blob_ids = [entry.object_id for entry in listed]
        blobs = repository.read_blobs(self.repository, blob_ids)
        expected = dict(entries)

        for entry in listed:
            with self.subTest(path=entry.path):
                self.assertEqual(blobs[entry.object_id], expected[entry.path])

    def test_read_blobs_rejects_unknown_non_blob_and_malformed_ids(self):
        result = self.publish()
        tree_id = (
            self.git("rev-parse", f"{result.commit_id}^{{tree}}")
            .stdout.strip()
            .decode("ascii")
        )
        for object_id in (
            "a" * 40,
            result.commit_id,
            tree_id,
            "HEAD",
            None,
        ):
            with self.subTest(object_id=object_id):
                with self.assertRaises(RepositoryError):
                    repository.read_blobs(self.repository, [object_id])

        self.assertEqual(repository.read_blobs(self.repository, []), {})

    def test_non_blob_tree_entries_have_no_size(self):
        published = self.publish()
        manifest_blob = self.git(
            "hash-object", "-w", "--stdin", input=b"manifest"
        ).stdout.strip().decode("ascii")
        payload = (
            f"100644 blob {manifest_blob}\tpicopg.json\n"
            f"160000 commit {published.commit_id}\tsub\n"
        ).encode("utf-8")
        tree_id = (
            self.git("mktree", input=payload)
            .stdout.strip()
            .decode("ascii")
        )
        commit_id = self.commit_tree(tree_id)
        entries = repository.list_commit_tree(self.repository, commit_id)
        by_path = {entry.path: entry for entry in entries}
        self.assertEqual(by_path["sub"].kind, "commit")
        self.assertIsNone(by_path["sub"].size)
        self.assertEqual(by_path["picopg.json"].size, len(b"manifest"))


class GitSubprocessIsolationTest(unittest.TestCase):
    """Direct checks of the child environment and override guard."""

    _SAFE_GIT_KEYS = {
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
    }

    def test_sanitized_environment_drops_every_inherited_git_variable(self):
        inherited = {
            "GIT_DIR": "/tmp/decoy",
            "GIT_COMMON_DIR": "/tmp/common",
            "GIT_OBJECT_DIRECTORY": "/tmp/objects",
            "GIT_CONFIG_PARAMETERS": "'core.hooksPath=/tmp/hooks'",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.bare",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_TRACE": "/tmp/trace",
            "GIT_TRACE2_EVENT": "/tmp/trace2",
            "GIT_TRACE_PACK_ACCESS": "/tmp/trace3",
            # A novel variable must also be dropped: the filter is not a
            # blacklist that a future Git version can outgrow.
            "GIT_UNKNOWN_FUTURE_VARIABLE": "1",
            "UNRELATED": "keep",
        }
        with mock.patch.dict(os.environ, inherited):
            environment = repository._sanitized_git_environment()

        self.assertEqual(environment["UNRELATED"], "keep")
        for key in environment:
            if key.startswith("GIT_"):
                self.assertIn(key, self._SAFE_GIT_KEYS)
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(environment["GIT_CONFIG_SYSTEM"], os.devnull)

    def test_only_commit_identity_overrides_are_accepted(self):
        for override in (
            {"GIT_DIR": "/tmp/decoy"},
            {"GIT_CONFIG_PARAMETERS": "'core.hooksPath=/tmp/hooks'"},
            {"PATH": "/usr/bin"},
            {"GIT_AUTHOR_NAME": "ok", "GIT_OBJECT_DIRECTORY": "/tmp/x"},
        ):
            with self.subTest(override=override):
                with self.assertRaises(RepositoryError):
                    repository._run_git(
                        ("rev-parse", "--is-bare-repository"),
                        environment=override,
                    )

    def test_commit_identity_override_is_allowed(self):
        environment = {
            "GIT_AUTHOR_NAME": "n",
            "GIT_AUTHOR_EMAIL": "e@x",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_NAME": "n",
            "GIT_COMMITTER_EMAIL": "e@x",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
        }
        with tempfile.TemporaryDirectory() as directory:
            completed = repository._run_git(
                ("rev-parse", "--is-bare-repository"),
                cwd=directory,
                environment=environment,
            )
        # The command fails outside a repository, but the environment guard
        # accepted all six identity fields instead of rejecting them.
        self.assertIsInstance(completed, subprocess.CompletedProcess)
        self.assertNotEqual(completed.returncode, 0)


class PendingRefTest(unittest.TestCase):
    """pending ref 只为一件事存在：让还没被数据库接受的 commit 不被 GC 收走。"""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "picopg.git"
        self.repository = repository.open_repository(self.path)
        self.metadata = repository.CommitMetadata(
            "o", "o@x", "2026-01-01T00:00:00Z", "m"
        )

    def bundle(self, body):
        return bundles.validate_bundle(
            [
                bundles.BundleEntry("picopg.json", b'{"schemaVersion":1,"runtime":"python"}'),
                bundles.BundleEntry("course/index.html", body),
            ]
        )

    def publish(self, playground_id="pg", body=b"one"):
        return repository.publish_playground(
            self.repository, playground_id, self.bundle(body), self.metadata
        )

    def refs(self, prefix="refs/picopg/"):
        completed = subprocess.run(
            ["git", "-C", str(self.path), "for-each-ref", "--format=%(refname)", prefix],
            capture_output=True, text=True, check=True,
        )
        return [line for line in completed.stdout.split("\n") if line]

    def test_publishing_creates_a_history_ref_and_leaves_nothing_pending(self):
        result = self.publish()
        self.assertEqual(self.refs(), ["refs/picopg/history/pg"])
        self.assertEqual(self.refs("refs/picopg/pending/"), [])
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.path), "rev-parse", "refs/picopg/history/pg"],
                capture_output=True, text=True, check=True,
            ).stdout.strip(),
            result.commit_id,
        )

    def test_a_pending_ref_keeps_an_unaccepted_commit_alive_and_can_go(self):
        result = self.publish()
        repository.create_pending_ref(self.repository, "evt1", result.commit_id)
        self.assertEqual(repository.list_pending_refs(self.repository), [("evt1", result.commit_id)])

        repository.delete_pending_ref(self.repository, "evt1")
        self.assertEqual(repository.list_pending_refs(self.repository), [])

    def test_deleting_a_missing_pending_ref_is_not_an_error(self):
        # 失败路径会删它，而那条路径可能已经被别的清理删过了
        repository.delete_pending_ref(self.repository, "never-existed")

    def test_converging_moves_the_history_ref(self):
        first = self.publish()
        second = self.publish(body=b"two")
        self.assertNotEqual(first.commit_id, second.commit_id)

        repository.converge_history_ref(self.repository, "pg", first.commit_id)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.path), "rev-parse", "refs/picopg/history/pg"],
                capture_output=True, text=True, check=True,
            ).stdout.strip(),
            first.commit_id,
        )

    def test_converging_rejects_something_that_is_not_a_commit(self):
        self.publish()
        for hostile in ("HEAD", "main", "", "0" * 39):
            with self.subTest(hostile=hostile):
                with self.assertRaises(repository.RepositoryError):
                    repository.converge_history_ref(self.repository, "pg", hostile)


class LegacyRefMigrationTest(unittest.TestCase):
    """旧的 `refs/heads/playgrounds/*` 要搬进新命名空间，且不能弄丢字节。"""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "picopg.git"
        self.repository = repository.open_repository(self.path)
        self.metadata = repository.CommitMetadata("o", "o@x", "2026-01-01T00:00:00Z", "m")
        self.bundle = bundles.validate_bundle(
            [
                bundles.BundleEntry("picopg.json", b'{"schemaVersion":1,"runtime":"python"}'),
                bundles.BundleEntry("course/index.html", b"one"),
            ]
        )

    def publish_with_legacy_ref(self, playground_id="pg", body=None):
        """造一个"旧代码发布过"的状态：commit + refs/heads 上的 ref。"""

        bundle = self.bundle if body is None else bundles.validate_bundle(
            [
                bundles.BundleEntry("picopg.json", b'{"schemaVersion":1,"runtime":"python"}'),
                bundles.BundleEntry("course/index.html", body),
            ]
        )
        result = repository.publish_playground(
            self.repository, playground_id, bundle, self.metadata
        )
        subprocess.run(
            ["git", "-C", str(self.path), "update-ref", "-d",
             f"refs/picopg/history/{playground_id}"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.path), "update-ref",
             f"refs/heads/playgrounds/{playground_id}", result.commit_id], check=True,
        )
        return result.commit_id

    def refs(self, prefix):
        completed = subprocess.run(
            ["git", "-C", str(self.path), "for-each-ref", "--format=%(refname)", prefix],
            capture_output=True, text=True, check=True,
        )
        return [line for line in completed.stdout.split("\n") if line]

    def test_a_legacy_ref_moves_and_the_commit_stays_readable(self):
        commit_id = self.publish_with_legacy_ref()
        self.assertEqual(repository.migrate_legacy_refs(self.repository, ["pg"]), 1)

        self.assertEqual(self.refs("refs/heads/"), [], "旧 ref 不该留下")
        self.assertEqual(self.refs("refs/picopg/history/"), ["refs/picopg/history/pg"])
        self.assertTrue(
            repository.commit_in_playground_history(self.repository, "pg", commit_id),
            "迁移后这个 Version 仍然可见",
        )

    def test_it_is_idempotent(self):
        self.publish_with_legacy_ref()
        self.assertEqual(repository.migrate_legacy_refs(self.repository, ["pg"]), 1)
        self.assertEqual(repository.migrate_legacy_refs(self.repository, ["pg"]), 0)

    def test_a_playground_with_no_legacy_ref_is_untouched(self):
        result = repository.publish_playground(
            self.repository, "pg", self.bundle, self.metadata
        )
        self.assertEqual(repository.migrate_legacy_refs(self.repository, ["pg"]), 0)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.path), "rev-parse", "refs/picopg/history/pg"],
                capture_output=True, text=True, check=True,
            ).stdout.strip(),
            result.commit_id,
            "已有的 history ref 是权威的，迁移不能覆盖它",
        )

    def test_the_history_ref_wins_when_the_legacy_one_is_stale(self):
        # 真实场景：旧命名空间的 ref 停在更早的 commit（迁移中断过、或那段历史
        # 后来又被发布过）。history ref 是当前被接受的版本，迁移不能把它拉回去。
        newer = self.publish_with_legacy_ref(body=b"two")
        older = self.publish_with_legacy_ref(body=b"one")
        subprocess.run(
            ["git", "-C", str(self.path), "update-ref",
             "refs/heads/playgrounds/pg", older], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.path), "update-ref",
             "refs/picopg/history/pg", newer], check=True,
        )

        repository.migrate_legacy_refs(self.repository, ["pg"])
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.path), "rev-parse", "refs/picopg/history/pg"],
                capture_output=True, text=True, check=True,
            ).stdout.strip(),
            newer,
            "history ref 是权威的，不能被旧的拉回去",
        )
        self.assertEqual(self.refs("refs/heads/"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
