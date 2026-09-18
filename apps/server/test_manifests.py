"""Tests for deterministic commit manifest generation.

The tests build real bare-repository history and real database records, then
exercise the exact-commit guarantee, path ordering, binary and empty blobs,
authorization, malformed trees, duplicates, and the size limits.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Allow ``import manifests`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bundles  # noqa: E402
import manifests  # noqa: E402
import playgrounds  # noqa: E402
import repository  # noqa: E402
from database import open_database  # noqa: E402


MANIFEST_BYTES = b'{"schemaVersion":1,"runtime":"python"}'


class ManifestTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        base = Path(self.temporary_directory.name)
        self.connection = open_database(base / "app.db")
        self.addCleanup(self.connection.close)
        self.repository_path = base / "picopg.git"
        self.repository = repository.open_repository(self.repository_path)


    # ----- database helpers ---------------------------------------------

    def create_playground(self, playground_id):
        return playgrounds.create_playground(self.connection, playground_id)

    def make_bundle(self, entries):
        return bundles.validate_bundle(
            [bundles.BundleEntry(path, data) for path, data in entries]
        )

    def default_entries(self, course_data=b"print(1)"):
        return [
            ("picopg.json", MANIFEST_BYTES),
            ("course/module/app.py", course_data),
            ("starter/setup.py", b"setup"),
        ]

    def metadata(self, message="publish playground"):
        return repository.CommitMetadata(
            name="Test Author",
            email="author@example.test",
            timestamp="2026-01-01T00:00:00+00:00",
            message=message,
        )

    def publish(self, playground_id, course_data=b"print(1)"):
        return playgrounds.publish_playground(
            self.connection,
            playground_id,
            self.repository,
            self.make_bundle(self.default_entries(course_data)),
            self.metadata(f"publish {playground_id}"),
        )

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

    def hash_blob(self, data):
        return self.git(
            "hash-object", "-w", "--stdin", input=data
        ).strip().decode("ascii")

    def write_tree(self, files):
        root = {}
        for path, data in files.items():
            node = root
            parts = path.split("/")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = data
        return self._write_tree_node(root)

    def _write_tree_node(self, node):
        lines = []
        for name in sorted(node):
            value = node[name]
            if isinstance(value, dict):
                object_id = self._write_tree_node(value)
                lines.append(f"040000 tree {object_id}\t{name}")
            else:
                object_id = self.hash_blob(value)
                lines.append(f"100644 blob {object_id}\t{name}")
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        return self.git("mktree", input=payload).strip().decode("ascii")

    def raw_tree(self, lines):
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        return self.git("mktree", input=payload).strip().decode("ascii")

    def commit_tree(self, tree_id, message=b"crafted\n", parent=None):
        arguments = ["commit-tree", tree_id]
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
            self.git(
                *arguments,
                input=message,
                environment=environment,
            )
            .strip()
            .decode("ascii")
        )

    def craft_commit(self, files, message=b"crafted\n"):
        return self.commit_tree(self.write_tree(files), message=message)

    def set_ref(self, playground_id, commit_id):
        self.git(
            "update-ref",
            self.repository.playground_ref(playground_id),
            commit_id,
        )

    # ----- manifest helpers ----------------------------------------------

    def generate(self, playground_id, commit_id):
        return manifests.generate_manifest(
            self.repository, playground_id, commit_id
        )

    def generated_file(self, manifest, path):
        for manifest_file in manifest.files:
            if manifest_file.path == path:
                return manifest_file
        self.fail(f"path not present in manifest: {path}")

    def assert_read_not_attempted(self):
        """Patch the content reads so any access fails the test."""

        def forbidden(*arguments, **kwargs):
            raise AssertionError("commit content was read")

        patcher_tree = mock.patch.object(
            manifests.repository_module, "list_commit_tree", forbidden
        )
        patcher_blobs = mock.patch.object(
            manifests.repository_module, "read_blobs", forbidden
        )
        patcher_tree.start()
        patcher_blobs.start()
        self.addCleanup(patcher_tree.stop)
        self.addCleanup(patcher_blobs.stop)

    def assert_blobs_not_read(self):
        """Patch only the blob reads, so tree metadata may still be listed."""

        def forbidden(*arguments, **kwargs):
            raise AssertionError("blob content was read")

        patcher = mock.patch.object(
            manifests.repository_module, "read_blobs", forbidden
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class ManifestGenerationTest(ManifestTestBase):
    def setUp(self):
        super().setUp()
        self.playground_id = "pg-manifest"
        self.create_playground(self.playground_id)

    # ----- happy paths ---------------------------------------------------

    def test_manifest_has_exact_commit_and_path_ordered_files(self):
        result = self.publish(self.playground_id, b"print(1)")
        manifest = self.generate(self.playground_id, result.commit_id)

        self.assertIsInstance(manifest, manifests.CommitManifest)
        self.assertEqual(manifest.commit_id, result.commit_id)

        paths = [manifest_file.path for manifest_file in manifest.files]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(
            paths,
            ["course/module/app.py", "picopg.json", "starter/setup.py"],
        )

        expected = {
            "picopg.json": MANIFEST_BYTES,
            "course/module/app.py": b"print(1)",
            "starter/setup.py": b"setup",
        }
        for path, data in expected.items():
            manifest_file = self.generated_file(manifest, path)
            self.assertEqual(manifest_file.size, len(data))
            self.assertEqual(
                manifest_file.sha256, hashlib.sha256(data).hexdigest()
            )
            self.assertEqual(manifest_file.blob_id, self.hash_blob(data))

    def test_mapping_and_serialization_are_canonical(self):
        result = self.publish(self.playground_id)
        manifest = self.generate(self.playground_id, result.commit_id)

        mapping = manifests.manifest_to_mapping(manifest)
        self.assertEqual(mapping["commitId"], result.commit_id)
        self.assertEqual(
            list(mapping["files"][0]), ["path", "size", "sha256", "blobId"]
        )
        self.assertEqual(
            [entry["path"] for entry in mapping["files"]],
            [manifest_file.path for manifest_file in manifest.files],
        )

        # The mapping is the one canonical view of a manifest; anything that
        # encodes it (the generated cache) must agree with it exactly.
        self.assertEqual(json.loads(json.dumps(mapping, sort_keys=True)), mapping)

    def test_generation_is_deterministic(self):
        result = self.publish(self.playground_id)
        first = self.generate(self.playground_id, result.commit_id)
        second = self.generate(self.playground_id, result.commit_id)

        self.assertEqual(first, second)
        self.assertEqual(
            manifests.manifest_to_mapping(first),
            manifests.manifest_to_mapping(second),
        )

    def test_old_commit_is_used_after_head_advances(self):
        first = self.publish(self.playground_id, b"first")
        second = self.publish(self.playground_id, b"second")
        self.assertNotEqual(first.commit_id, second.commit_id)

        first_manifest = self.generate(self.playground_id, first.commit_id)
        second_manifest = self.generate(self.playground_id, second.commit_id)

        # The manifest pins the requested commit, never the current HEAD.
        self.assertEqual(first_manifest.commit_id, first.commit_id)
        self.assertEqual(second_manifest.commit_id, second.commit_id)

        first_file = self.generated_file(
            first_manifest, "course/module/app.py"
        )
        second_file = self.generated_file(
            second_manifest, "course/module/app.py"
        )
        self.assertEqual(
            first_file.sha256, hashlib.sha256(b"first").hexdigest()
        )
        self.assertEqual(
            second_file.sha256, hashlib.sha256(b"second").hexdigest()
        )

    def test_no_checkout_head_lookup_or_temp_copy(self):
        result = self.publish(self.playground_id)
        observed = []
        original = repository._run_git

        def spy(arguments, cwd=None, stdin=None, environment=None):
            observed.append(tuple(arguments))
            return original(
                arguments, cwd=cwd, stdin=stdin, environment=environment
            )

        with mock.patch.object(repository, "_run_git", side_effect=spy):
            self.generate(self.playground_id, result.commit_id)

        for arguments in observed:
            self.assertNotIn("HEAD", arguments)
            self.assertNotIn("checkout", arguments)
            self.assertNotIn("clone", arguments)
            self.assertNotIn("archive", arguments)
        commands = {arguments[0] for arguments in observed if arguments}
        self.assertIn("ls-tree", commands)
        self.assertIn("cat-file", commands)

    # ----- published history ---------------------------------------------

    def test_binary_and_empty_blobs_use_exact_bytes(self):
        binary = bytes(range(256))
        self.create_playground("pg-bytes")
        result = playgrounds.publish_playground(
            self.connection,
            "pg-bytes",
            self.repository,
            self.make_bundle(
                [
                    ("picopg.json", MANIFEST_BYTES),
                    ("course/empty.txt", b""),
                    ("course/data.bin", binary),
                ]
            ),
            self.metadata(),
        )
        manifest = self.generate("pg-bytes", result.commit_id)

        self.assertEqual(self.generated_file(manifest, "course/empty.txt").size, 0)
        self.assertEqual(self.generated_file(manifest, "course/data.bin").size, len(binary))

    def test_a_commit_from_another_playground_is_refused(self):
        self.create_playground("pg-other")
        other = self.publish("pg-other")

        with self.assertRaises(manifests.ManifestError) as caught:
            self.generate(self.playground_id, other.commit_id)
        self.assertEqual(
            caught.exception.reason, manifests.REASON_COMMIT_NOT_PUBLISHED
        )

    def test_a_missing_commit_is_refused_before_any_content_read(self):
        self.assert_read_not_attempted()

        with self.assertRaises(manifests.ManifestError) as caught:
            self.generate(self.playground_id, "a" * 40)
        self.assertEqual(
            caught.exception.reason, manifests.REASON_COMMIT_NOT_PUBLISHED
        )

    def test_non_blob_gitlink_entry_is_rejected(self):
        published = self.publish(self.playground_id)
        manifest_blob = self.hash_blob(MANIFEST_BYTES)
        file_blob = self.hash_blob(b"file")
        course_tree = self.raw_tree(
            [
                f"100644 blob {file_blob}\tfile.py",
                f"160000 commit {published.commit_id}\tsub",
            ]
        )
        tree = self.raw_tree(
            [
                f"040000 tree {course_tree}\tcourse",
                f"100644 blob {manifest_blob}\tpicopg.json",
            ]
        )
        commit = self.commit_tree(tree)
        self.set_ref(self.playground_id, commit)

        with self.assertRaises(manifests.ManifestError) as caught:
            self.generate(self.playground_id, commit)
        self.assertEqual(
            caught.exception.reason, manifests.REASON_ENTRY_NOT_BLOB
        )

    def test_symlink_entry_is_rejected(self):
        manifest_blob = self.hash_blob(MANIFEST_BYTES)
        link_blob = self.hash_blob(b"target")
        starter_tree = self.raw_tree(
            [f"120000 blob {link_blob}\tlink"]
        )
        tree = self.raw_tree(
            [
                f"100644 blob {manifest_blob}\tpicopg.json",
                f"040000 tree {starter_tree}\tstarter",
            ]
        )
        commit = self.commit_tree(tree)
        self.set_ref(self.playground_id, commit)

        with self.assertRaises(manifests.ManifestError) as caught:
            self.generate(self.playground_id, commit)
        self.assertEqual(
            caught.exception.reason, manifests.REASON_ENTRY_MODE_NOT_FILE
        )

    def test_path_outside_the_allowlist_is_rejected(self):
        commit = self.craft_commit(
            {
                "picopg.json": MANIFEST_BYTES,
                "course/ok.py": b"ok",
                "secrets/keys.txt": b"nope",
            }
        )
        self.set_ref(self.playground_id, commit)

        with self.assertRaises(manifests.ManifestError) as caught:
            self.generate(self.playground_id, commit)
        self.assertEqual(
            caught.exception.reason, manifests.REASON_PATH_NOT_ALLOWED
        )

    def test_work_path_is_rejected(self):
        commit = self.craft_commit(
            {
                "picopg.json": MANIFEST_BYTES,
                "work/main.py": b"work",
            }
        )
        self.set_ref(self.playground_id, commit)

        with self.assertRaises(manifests.ManifestError) as caught:
            self.generate(self.playground_id, commit)
        self.assertEqual(
            caught.exception.reason, manifests.REASON_PATH_NOT_ALLOWED
        )

    def test_duplicate_path_is_rejected(self):
        manifest_blob = self.hash_blob(MANIFEST_BYTES)
        tree = self.raw_tree(
            [
                f"100644 blob {manifest_blob}\tpicopg.json",
                f"100644 blob {manifest_blob}\tpicopg.json",
            ]
        )
        commit = self.commit_tree(tree)
        self.set_ref(self.playground_id, commit)

        with self.assertRaises(manifests.ManifestError) as caught:
            self.generate(self.playground_id, commit)
        self.assertEqual(
            caught.exception.reason, manifests.REASON_DUPLICATE_PATH
        )

    def test_legacy_reserved_paths_are_rejected(self):
        for path in (
            "course/CON.py",
            "course/.git/config",
            "course/a:b.py",
            "course/trailing.",
        ):
            with self.subTest(path=path):
                commit = self.craft_commit(
                    {"picopg.json": MANIFEST_BYTES, path: b"x"}
                )
                self.set_ref(self.playground_id, commit)
                with self.assertRaises(manifests.ManifestError) as caught:
                    self.generate(self.playground_id, commit)
                self.assertEqual(
                    caught.exception.reason,
                    manifests.REASON_PATH_NOT_ALLOWED,
                )

    def test_legacy_casefold_collision_is_rejected(self):
        commit = self.craft_commit(
            {
                "picopg.json": MANIFEST_BYTES,
                "course/File.py": b"1",
                "course/file.py": b"2",
            }
        )
        self.set_ref(self.playground_id, commit)
        with self.assertRaises(manifests.ManifestError) as caught:
            self.generate(self.playground_id, commit)
        self.assertEqual(
            caught.exception.reason, manifests.REASON_PATH_CASE_COLLISION
        )

    def test_legacy_non_portable_read_is_rejected(self):
        # ``read_commit_file`` resolves paths through ``normalize_content_path``,
        # so a non-portable request is rejected before any tree or blob read.
        for path in ("course/CON.py", "course/.git/config", "course/a:b.py"):
            with self.subTest(path=path):
                with self.assertRaises(manifests.ManifestError) as caught:
                    manifests.normalize_content_path(path)
                self.assertEqual(
                    caught.exception.reason,
                    manifests.REASON_PATH_NOT_ALLOWED,
                )

    def test_entry_count_limit_is_enforced(self):
        result = self.publish(self.playground_id)
        with mock.patch.object(manifests, "MAX_ENTRY_COUNT", 2):
            with self.assertRaises(manifests.ManifestError) as caught:
                self.generate(self.playground_id, result.commit_id)
        self.assertEqual(
            caught.exception.reason, manifests.REASON_TOO_MANY_ENTRIES
        )

    def test_file_size_limit_is_enforced_before_content_read(self):
        oversized = b"x" * (manifests.MAX_FILE_BYTES + 1)
        commit = self.craft_commit(
            {
                "picopg.json": MANIFEST_BYTES,
                "course/big.bin": oversized,
            }
        )
        self.set_ref(self.playground_id, commit)
        self.assert_blobs_not_read()

        with self.assertRaises(manifests.ManifestError) as caught:
            self.generate(self.playground_id, commit)
        self.assertEqual(
            caught.exception.reason, manifests.REASON_FILE_TOO_LARGE
        )

    def test_total_size_limit_is_enforced_before_content_read(self):
        chunk = b"y" * manifests.MAX_FILE_BYTES
        files = {"picopg.json": MANIFEST_BYTES}
        for index in range(9):
            files[f"course/chunk-{index}.bin"] = chunk
        commit = self.craft_commit(files)
        self.set_ref(self.playground_id, commit)
        self.assert_blobs_not_read()

        with self.assertRaises(manifests.ManifestError) as caught:
            self.generate(self.playground_id, commit)
        self.assertEqual(
            caught.exception.reason, manifests.REASON_BUNDLE_TOO_LARGE
        )

    # ----- corrupt object store ------------------------------------------

    def test_missing_blob_object_is_rejected(self):
        result = self.publish(self.playground_id)
        with mock.patch.object(
            manifests.repository_module, "read_blobs", return_value={}
        ):
            with self.assertRaises(manifests.ManifestError) as caught:
                self.generate(self.playground_id, result.commit_id)
        self.assertEqual(
            caught.exception.reason, manifests.REASON_MISSING_BLOB
        )

    def test_blob_size_mismatch_is_rejected(self):
        result = self.publish(self.playground_id)

        def truncated(repository, object_ids):
            return {object_id: b"" for object_id in object_ids}

        with mock.patch.object(
            manifests.repository_module, "read_blobs", side_effect=truncated
        ):
            with self.assertRaises(manifests.ManifestError) as caught:
                self.generate(self.playground_id, result.commit_id)
        self.assertEqual(
            caught.exception.reason, manifests.REASON_BLOB_SIZE_MISMATCH
        )


class CommitFileReadTest(ManifestTestBase):
    def setUp(self):
        super().setUp()
        self.playground_id = "pg-file"
        self.create_playground(self.playground_id)

    def read(self, playground_id, commit_id, path):
        return manifests.read_commit_file(
            self.repository, playground_id, commit_id, path
        )

    def publish_entries(self, playground_id, entries):
        return playgrounds.publish_playground(
            self.connection,
            playground_id,
            self.repository,
            self.make_bundle(entries),
            self.metadata(),
        )

    def test_normalize_content_path_uses_bundle_rules(self):
        self.assertEqual(
            manifests.normalize_content_path("course/module/app.py"),
            "course/module/app.py",
        )
        self.assertEqual(
            manifests.normalize_content_path("picopg.json"),
            "picopg.json",
        )
        # The bundle rules apply Unicode NFC normalization exactly once.
        self.assertEqual(
            manifests.normalize_content_path("course/cafe\u0301.py"),
            "course/caf\u00e9.py",
        )

        for path in (
            "../course/app.py",
            "course/../app.py",
            "/course/app.py",
            "course\\app.py",
            "work/app.py",
            ".picopg/work.json",
            "other.txt",
            "course/",
            "",
            "course/\x00app.py",
            None,
            7,
        ):
            with self.subTest(path=path):
                with self.assertRaises(manifests.ManifestError) as caught:
                    manifests.normalize_content_path(path)
                self.assertEqual(
                    caught.exception.reason,
                    manifests.REASON_PATH_NOT_ALLOWED,
                )

    def test_read_returns_exact_bytes_and_hash(self):
        result = self.publish(self.playground_id)
        content = self.read(
            self.playground_id, result.commit_id, "course/module/app.py"
        )

        self.assertIsInstance(content, manifests.CommitFile)
        self.assertEqual(content.path, "course/module/app.py")
        self.assertEqual(content.data, b"print(1)")
        self.assertEqual(
            content.sha256, hashlib.sha256(b"print(1)").hexdigest()
        )

    def test_binary_and_empty_files_round_trip(self):
        binary = bytes(range(256))
        entries = [
            ("picopg.json", MANIFEST_BYTES),
            ("course/empty.txt", b""),
            ("course/data.bin", binary),
        ]
        result = self.publish_entries(self.playground_id, entries)

        empty = self.read(
            self.playground_id, result.commit_id, "course/empty.txt"
        )
        self.assertEqual(empty.data, b"")
        self.assertEqual(empty.sha256, hashlib.sha256(b"").hexdigest())

        data = self.read(
            self.playground_id, result.commit_id, "course/data.bin"
        )
        self.assertEqual(data.data, binary)
        self.assertEqual(data.sha256, hashlib.sha256(binary).hexdigest())

    def test_nfc_request_resolves_nfc_tree_path(self):
        entries = [
            ("picopg.json", MANIFEST_BYTES),
            ("course/caf\u00e9.py", b"nfc"),
        ]
        result = self.publish_entries(self.playground_id, entries)
        content = self.read(
            self.playground_id, result.commit_id, "course/cafe\u0301.py"
        )
        self.assertEqual(content.path, "course/caf\u00e9.py")
        self.assertEqual(content.data, b"nfc")

    def test_missing_path_is_not_found(self):
        result = self.publish(self.playground_id)
        with self.assertRaises(manifests.ManifestError) as caught:
            self.read(
                self.playground_id, result.commit_id, "course/missing.py"
            )
        self.assertEqual(
            caught.exception.reason, manifests.REASON_PATH_NOT_FOUND
        )

    def test_path_is_resolved_in_the_requested_commit_only(self):
        first = self.publish_entries(
            self.playground_id,
            [
                ("picopg.json", MANIFEST_BYTES),
                ("course/first.py", b"first"),
            ],
        )
        second = self.publish_entries(
            self.playground_id,
            [
                ("picopg.json", MANIFEST_BYTES),
                ("course/second.py", b"second"),
            ],
        )

        self.assertEqual(
            self.read(
                self.playground_id, first.commit_id, "course/first.py"
            ).data,
            b"first",
        )
        # The path exists historically but not in the exact requested commit.
        with self.assertRaises(manifests.ManifestError) as caught:
            self.read(
                self.playground_id, second.commit_id, "course/first.py"
            )
        self.assertEqual(
            caught.exception.reason, manifests.REASON_PATH_NOT_FOUND
        )

    def test_non_file_entry_at_allowed_path_is_rejected(self):
        manifest_blob = self.hash_blob(MANIFEST_BYTES)
        link_blob = self.hash_blob(b"target")
        starter_tree = self.raw_tree([f"120000 blob {link_blob}\tlink"])
        tree = self.raw_tree(
            [
                f"100644 blob {manifest_blob}\tpicopg.json",
                f"040000 tree {starter_tree}\tstarter",
            ]
        )
        commit = self.commit_tree(tree)
        self.set_ref(self.playground_id, commit)

        with self.assertRaises(manifests.ManifestError) as caught:
            self.read(self.playground_id, commit, "starter/link")
        self.assertEqual(
            caught.exception.reason, manifests.REASON_ENTRY_MODE_NOT_FILE
        )

    def test_current_playground_commit_tracks_published_head(self):
        empty_id = "pg-empty-file"
        self.create_playground(empty_id)
        self.assertIsNone(
            manifests.current_playground_commit(self.repository, empty_id)
        )

        result = self.publish(self.playground_id)
        self.assertEqual(
            manifests.current_playground_commit(
                self.repository, self.playground_id
            ),
            result.commit_id,
        )
        self.assertIsNone(
            manifests.current_playground_commit(self.repository, "pg-missing")
        )

if __name__ == "__main__":
    unittest.main(verbosity=2)
