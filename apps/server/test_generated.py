"""Tests for the rebuildable public caches.

The tests build a real bare repository, real SQLite records and a temporary
artifacts directory, then exercise deterministic rebuild, delete/rebuild,
failure before replacement, and artifact mutation.  A generated cache is a
derived copy: deleting it must always be recoverable from authority.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

# Allow ``import generated`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifacts  # noqa: E402
import bundles  # noqa: E402
import generated  # noqa: E402
import manifests  # noqa: E402
import playgrounds  # noqa: E402
import repository  # noqa: E402
from database import open_database  # noqa: E402


MANIFEST_BYTES = b'{"schemaVersion":1,"runtime":"python"}'


class GeneratedCacheTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        base = Path(self.temporary_directory.name)
        self.connection = open_database(base / "app.db")
        self.addCleanup(self.connection.close)
        self.repository = repository.open_repository(base / "picopg.git")
        self.artifacts_path = base / "artifacts"
        self.generated_path = base / "generated"


    # ----- fixtures ------------------------------------------------------

    def publish(self, playground_id):
        playground = playgrounds.create_playground(
            self.connection, playground_id
        )
        bundle = bundles.validate_bundle(
            [
                bundles.BundleEntry("picopg.json", MANIFEST_BYTES),
                bundles.BundleEntry(
                    "course/module/app.py",
                    f"print({playground_id!r})".encode("utf-8"),
                ),
            ]
        )
        result = playgrounds.publish_playground(
            self.connection,
            playground_id,
            self.repository,
            bundle,
            repository.CommitMetadata(
                name="Test Author",
                email="author@example.test",
                timestamp="2026-01-01T00:00:00+00:00",
                message=f"publish {playground_id}",
            ),
        )
        return playground, result.commit_id

    def write_artifact(self, version, filename, data):
        directory = self.artifacts_path / version
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_bytes(data)

    def rebuild(self):
        return generated.rebuild(
            self.generated_path,
            connection=self.connection,
            repository=self.repository,
            artifacts_path=self.artifacts_path,
        )

    def read_generated(self):
        generation = generated.resolve_current_generation(self.generated_path)
        return {
            generated.BOOTSTRAP_FILENAME: generation.bootstrap,
            generated.ARTIFACT_INDEX_FILENAME: generation.artifact_index,
        }

    def generation_payloads(self, payloads):
        return {
            generated.BOOTSTRAP_FILENAME: payloads[
                generated.BOOTSTRAP_FILENAME
            ],
            generated.ARTIFACT_INDEX_FILENAME: payloads[
                generated.ARTIFACT_INDEX_FILENAME
            ],
        }

    # ----- tests ---------------------------------------------------------

    def test_rebuild_is_deterministic(self):
        self.publish(f"public-{uuid.uuid4().hex[:8]}")
        self.write_artifact("v1", "runtime.exe", b"runtime")

        first = self.rebuild()
        second = self.rebuild()

        self.assertEqual(first, second)
        self.assertEqual(
            self.read_generated(), self.generation_payloads(first)
        )

    def test_delete_all_generated_files_then_rebuild_restores_them(self):
        self.publish(f"public-{uuid.uuid4().hex[:8]}")
        self.write_artifact("v1", "runtime.exe", b"runtime")
        first = self.rebuild()

        shutil.rmtree(self.generated_path)
        with self.assertRaises(generated.GeneratedCacheError):
            self.read_generated()

        second = self.rebuild()
        self.assertEqual(first, second)
        self.assertEqual(
            self.read_generated(), self.generation_payloads(second)
        )

    def test_publish_is_deterministic_and_pointer_is_durable(self):
        self.publish(f"public-{uuid.uuid4().hex[:8]}")
        self.write_artifact("v1", "runtime.exe", b"runtime")
        payloads = self.rebuild()

        # The pointer is canonical JSON naming the content-derived id.
        pointer = (self.generated_path / generated.CURRENT_FILENAME).read_bytes()
        self.assertTrue(pointer.endswith(b"\n"))
        document = json.loads(pointer)
        expected = generated.generation_id(
            payloads[generated.BOOTSTRAP_FILENAME],
            payloads[generated.ARTIFACT_INDEX_FILENAME],
        )
        self.assertEqual(document, {"generation": expected})
        self.assertTrue(
            (
                self.generated_path
                / generated.GENERATIONS_DIRNAME
                / expected
            ).is_dir()
        )

    def test_identical_rebuild_reuses_one_generation(self):
        self.publish(f"public-{uuid.uuid4().hex[:8]}")
        self.write_artifact("v1", "runtime.exe", b"runtime")
        self.rebuild()
        first = generated.resolve_current_generation(self.generated_path)
        self.rebuild()
        second = generated.resolve_current_generation(self.generated_path)
        self.assertEqual(first.generation_id, second.generation_id)
        entries = list(
            (self.generated_path / generated.GENERATIONS_DIRNAME).iterdir()
        )
        self.assertEqual(
            [entry.name for entry in entries], [first.generation_id]
        )

    def test_failed_pointer_replace_keeps_old_generation_current(self):
        self.publish(f"public-{uuid.uuid4().hex[:8]}")
        self.write_artifact("v1", "runtime.exe", b"runtime")
        self.rebuild()
        before = generated.resolve_current_generation(self.generated_path)

        # A second public Playground changes authority, so the next rebuild
        # builds a new generation and then fails while replacing the pointer.
        self.publish(f"public-{uuid.uuid4().hex[:8]}")
        original_replace = generated.os.replace

        def failing_replace(source, destination, *arguments, **keywords):
            if str(destination).endswith(generated.CURRENT_FILENAME):
                raise OSError("pointer replace failed")
            return original_replace(
                source, destination, *arguments, **keywords
            )

        with mock.patch.object(
            generated.os, "replace", side_effect=failing_replace
        ):
            with self.assertRaises(OSError):
                self.rebuild()

        after = generated.resolve_current_generation(self.generated_path)
        self.assertEqual(after.generation_id, before.generation_id)

    def test_corrupt_generation_is_rejected(self):
        self.publish(f"public-{uuid.uuid4().hex[:8]}")
        self.rebuild()
        identifier = generated.resolve_current_generation(
            self.generated_path
        ).generation_id
        target = (
            self.generated_path
            / generated.GENERATIONS_DIRNAME
            / identifier
            / generated.ARTIFACT_INDEX_FILENAME
        )
        target.write_bytes(b"{}\n")
        with self.assertRaises(generated.GeneratedCacheError):
            generated.resolve_current_generation(self.generated_path)

    def test_symlinked_generation_is_rejected(self):
        self.publish(f"public-{uuid.uuid4().hex[:8]}")
        self.rebuild()
        identifier = generated.resolve_current_generation(
            self.generated_path
        ).generation_id
        directory = (
            self.generated_path
            / generated.GENERATIONS_DIRNAME
            / identifier
        )
        shutil.rmtree(directory)
        directory.symlink_to(self.artifacts_path)
        with self.assertRaises(generated.GeneratedCacheError):
            generated.resolve_current_generation(self.generated_path)

    def test_failure_before_replacement_keeps_old_output(self):
        self.publish(f"public-{uuid.uuid4().hex[:8]}")
        self.write_artifact("v1", "runtime.exe", b"runtime")
        self.rebuild()
        before = self.read_generated()

        with mock.patch.object(
            manifests,
            "generate_manifest",
            side_effect=manifests.ManifestError("boom"),
        ):
            with self.assertRaises(manifests.ManifestError):
                self.rebuild()

        self.assertEqual(self.read_generated(), before)

    def test_atomic_write_failure_keeps_previous_file(self):
        target = self.generated_path / generated.CATALOG_FILENAME
        generated.atomic_write(target, b"old")

        with mock.patch(
            "generated.os.replace", side_effect=OSError("replace failed")
        ):
            with self.assertRaises(OSError):
                generated.atomic_write(target, b"new")

        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(
            sorted(path.name for path in target.parent.iterdir()),
            [generated.CATALOG_FILENAME],
        )

    def test_a_forged_cache_cannot_publish_an_unreachable_commit(self):
        # A cache is derived, so a hand-written catalog must not be able to
        # advertise a commit the Playground's ref cannot reach: the manifest
        # is generated from authority, not from the cache.
        playground_id = f"forged-{uuid.uuid4().hex[:8]}"
        _, commit_id = self.publish(playground_id)
        forged = {
            "catalog": [
                {"playgroundId": playground_id, "commitId": "0" * 40}
            ]
        }
        generated.atomic_write(
            self.generated_path / generated.CATALOG_FILENAME,
            json.dumps(forged, sort_keys=True).encode("utf-8"),
        )

        self.assertEqual(
            self.request_catalog_ids(),
            [(playground_id, commit_id)],
        )
        with self.assertRaises(manifests.ManifestError):
            manifests.generate_manifest(
                self.repository, playground_id, "0" * 40
            )

    def request_catalog_ids(self):
        catalog = generated.public_catalog(self.connection, self.repository)
        return [(item["playgroundId"], item["commitId"]) for item in catalog]

    def test_artifact_mutation_is_rejected_on_rebuild(self):
        self.write_artifact("v1", "runtime.exe", b"first")
        self.rebuild()

        self.write_artifact("v1", "runtime.exe", b"second")

        with self.assertRaises(artifacts.ArtifactError) as caught:
            self.rebuild()
        self.assertEqual(
            caught.exception.reason, artifacts.REASON_MUTABLE_REPLACEMENT
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
