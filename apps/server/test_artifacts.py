"""Tests for immutable runtime artifact indexing and verified reads."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Allow ``import artifacts`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifacts  # noqa: E402


class ArtifactIndexTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "artifacts"

    def write_artifact(self, version, filename, data):
        directory = self.root / version
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        path.write_bytes(data)
        return path

    def entry(self, version, filename, data):
        return artifacts.ArtifactEntry(
            version=version,
            filename=filename,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def test_valid_index_is_ordered_and_serves_exact_bytes(self):
        first = b"runtime-binary-one"
        second = b"runtime-binary-two"
        self.write_artifact("v2", "runtime.exe", second)
        self.write_artifact("v1", "runtime.exe", first)

        entries = artifacts.index_artifacts(self.root)

        self.assertEqual([item.version for item in entries], ["v1", "v2"])
        self.assertEqual(entries[0], self.entry("v1", "runtime.exe", first))
        self.assertEqual(
            artifacts.serve_artifact(self.root, entries[0]), first
        )
        self.assertEqual(
            artifacts.serve_artifact(self.root, entries[1]), second
        )

    def test_missing_directory_is_an_empty_index(self):
        self.assertEqual(artifacts.index_artifacts(self.root), [])

    def test_tampering_is_rejected_before_bytes_are_served(self):
        data = b"trusted-runtime"
        self.write_artifact("v1", "runtime.exe", data)
        entries = artifacts.index_artifacts(self.root)

        self.write_artifact("v1", "runtime.exe", b"tampered-runtime!")

        with self.assertRaises(artifacts.ArtifactError) as caught:
            artifacts.serve_artifact(self.root, entries[0])
        self.assertEqual(
            caught.exception.reason, artifacts.REASON_CHECKSUM_MISMATCH
        )

    def test_verify_artifact_checks_without_returning_bytes(self):
        data = b"trusted-runtime"
        self.write_artifact("v1", "runtime.exe", data)
        entry = artifacts.index_artifacts(self.root)[0]

        target = artifacts.verify_artifact(self.root, entry)
        self.assertEqual(target.read_bytes(), data)

        self.write_artifact("v1", "runtime.exe", b"tampered-runtime!")
        with self.assertRaises(artifacts.ArtifactError) as caught:
            artifacts.verify_artifact(self.root, entry)
        self.assertEqual(
            caught.exception.reason, artifacts.REASON_CHECKSUM_MISMATCH
        )

    def test_resized_file_is_rejected(self):
        data = b"trusted-runtime"
        self.write_artifact("v1", "runtime.exe", data)
        entry = artifacts.index_artifacts(self.root)[0]

        self.write_artifact("v1", "runtime.exe", data + b"!")

        with self.assertRaises(artifacts.ArtifactError) as caught:
            artifacts.serve_artifact(self.root, entry)
        self.assertEqual(
            caught.exception.reason, artifacts.REASON_CHECKSUM_MISMATCH
        )

    def test_symlinked_version_directory_cannot_escape(self):
        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir()
        (outside / "runtime.exe").write_bytes(b"outside")
        self.root.mkdir(parents=True)
        (self.root / "v1").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(artifacts.ArtifactError) as caught:
            artifacts.index_artifacts(self.root)
        self.assertEqual(caught.exception.reason, artifacts.REASON_PATH_ESCAPE)

    def test_symlinked_filename_cannot_escape(self):
        secret = Path(self.temporary_directory.name) / "secret"
        secret.write_bytes(b"secret")
        directory = self.root / "v1"
        directory.mkdir(parents=True)
        (directory / "runtime.exe").symlink_to(secret)

        with self.assertRaises(artifacts.ArtifactError) as caught:
            artifacts.index_artifacts(self.root)
        self.assertEqual(caught.exception.reason, artifacts.REASON_PATH_ESCAPE)

    def test_non_normalized_version_is_rejected(self):
        data = b"runtime"
        self.write_artifact("e\u0301", "runtime.exe", data)

        with self.assertRaises(artifacts.ArtifactError) as caught:
            artifacts.index_artifacts(self.root)
        self.assertEqual(
            caught.exception.reason, artifacts.REASON_INVALID_VERSION
        )

    def test_duplicate_version_is_rejected(self):
        directory = self.root / "v1"
        directory.mkdir(parents=True)
        (directory / "runtime.exe").write_bytes(b"one")
        (directory / "second.exe").write_bytes(b"two")

        with self.assertRaises(artifacts.ArtifactError) as caught:
            artifacts.index_artifacts(self.root)
        self.assertEqual(
            caught.exception.reason, artifacts.REASON_DUPLICATE_VERSION
        )

    def test_empty_version_directory_is_rejected(self):
        (self.root / "v1").mkdir(parents=True)

        with self.assertRaises(artifacts.ArtifactError) as caught:
            artifacts.index_artifacts(self.root)
        self.assertEqual(
            caught.exception.reason, artifacts.REASON_MISSING_ARTIFACT
        )

    def test_unexpected_root_file_is_rejected(self):
        self.root.mkdir(parents=True)
        (self.root / "loose.exe").write_bytes(b"loose")

        with self.assertRaises(artifacts.ArtifactError) as caught:
            artifacts.index_artifacts(self.root)
        self.assertEqual(
            caught.exception.reason, artifacts.REASON_UNEXPECTED_ENTRY
        )

    def test_mutable_replacement_is_rejected(self):
        self.write_artifact("v1", "runtime.exe", b"first")
        previous = artifacts.index_artifacts(self.root)

        self.write_artifact("v1", "runtime.exe", b"second")

        with self.assertRaises(artifacts.ArtifactError) as caught:
            artifacts.index_artifacts(self.root, previous)
        self.assertEqual(
            caught.exception.reason, artifacts.REASON_MUTABLE_REPLACEMENT
        )

    def test_identical_rebuild_accepts_previous_index(self):
        data = b"runtime"
        self.write_artifact("v1", "runtime.exe", data)
        previous = artifacts.index_artifacts(self.root)

        self.assertEqual(
            artifacts.index_artifacts(self.root, previous), previous
        )

    def test_serving_a_missing_file_is_rejected(self):
        data = b"runtime"
        entry = self.entry("v1", "runtime.exe", data)

        with self.assertRaises(artifacts.ArtifactError) as caught:
            artifacts.serve_artifact(self.root, entry)
        self.assertEqual(
            caught.exception.reason, artifacts.REASON_MISSING_ARTIFACT
        )

    def test_serving_traversal_entry_is_rejected(self):
        data = b"runtime"
        cases = (
            ("..", "runtime.exe", artifacts.REASON_INVALID_VERSION),
            ("v1", "../runtime.exe", artifacts.REASON_PATH_ESCAPE),
            ("v1", "..", artifacts.REASON_PATH_ESCAPE),
        )
        for version, filename, reason in cases:
            with self.subTest(version=version, filename=filename):
                entry = self.entry(version, filename, data)
                with self.assertRaises(artifacts.ArtifactError) as caught:
                    artifacts.serve_artifact(self.root, entry)
                self.assertEqual(caught.exception.reason, reason)

    def test_serving_never_executes_the_artifact(self):
        sentinel = Path(self.temporary_directory.name) / "sentinel"
        data = f"#!/bin/sh\ntouch {sentinel}\n".encode("utf-8")
        self.write_artifact("v1", "runtime.sh", data)
        entry = artifacts.index_artifacts(self.root)[0]

        self.assertEqual(artifacts.serve_artifact(self.root, entry), data)
        self.assertFalse(sentinel.exists())

    def test_index_mapping_round_trips(self):
        data = b"runtime"
        self.write_artifact("v1", "runtime.exe", data)
        entries = artifacts.index_artifacts(self.root)

        mapping = artifacts.index_to_mapping(entries)
        self.assertEqual(
            json.loads(json.dumps(mapping)), mapping
        )
        self.assertEqual(artifacts.mapping_to_index(mapping), entries)

    def test_malformed_index_is_rejected(self):
        bad_type = [
            {"version": 1, "filename": "a", "size": 0, "sha256": "x"}
        ]
        bad_size = [
            {"version": "v1", "filename": "a", "size": -1, "sha256": "x"}
        ]
        for malformed in (
            None,
            {},
            {"artifacts": {}},
            {"artifacts": [{"version": "v1"}]},
            {"artifacts": bad_type},
            {"artifacts": bad_size},
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaises(artifacts.ArtifactError) as caught:
                    artifacts.mapping_to_index(malformed)
                self.assertEqual(
                    caught.exception.reason, artifacts.REASON_INVALID_INDEX
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
