"""Tests for rebuildable public caches.

The tests build a real bare repository, real SQLite records and a temporary
artifacts directory, then exercise deterministic rebuild, delete/rebuild,
failure before replacement, artifact mutation, and the rule that private
authorization never depends on the generated cache.
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

# Allow ``import generated`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import accounts  # noqa: E402
import artifacts  # noqa: E402
import authorization  # noqa: E402
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

    # ----- fixtures ------------------------------------------------------

    def publish(self, playground_id, policy):
        playground = playgrounds.create_playground(
            self.connection, self.admin, playground_id
        )
        if policy == playgrounds.PUBLIC_POLICY:
            playgrounds.update_playground(
                self.connection,
                self.admin,
                playground_id,
                {"policy": playgrounds.PUBLIC_POLICY},
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
            self.admin,
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
        return {
            filename: (self.generated_path / filename).read_bytes()
            for filename in generated.GENERATED_FILENAMES
            if (self.generated_path / filename).is_file()
        }

    # ----- tests ---------------------------------------------------------

    def test_rebuild_writes_public_caches_and_excludes_private(self):
        public_id = f"public-{uuid.uuid4().hex[:8]}"
        private_id = f"private-{uuid.uuid4().hex[:8]}"
        _, public_commit = self.publish(
            public_id, playgrounds.PUBLIC_POLICY
        )
        _, private_commit = self.publish(
            private_id, playgrounds.RELEASE_ONLY_POLICY
        )
        self.write_artifact("v1", "runtime.exe", b"runtime")

        payloads = self.rebuild()

        catalog = json.loads(payloads[generated.CATALOG_FILENAME])
        self.assertEqual(
            catalog["catalog"],
            [{"playgroundId": public_id, "commitId": public_commit}],
        )
        self.assertNotIn(private_id, payloads[generated.CATALOG_FILENAME].decode())
        self.assertNotIn(
            private_commit, payloads[generated.CATALOG_FILENAME].decode()
        )

        artifact_index = json.loads(
            payloads[generated.ARTIFACT_INDEX_FILENAME]
        )
        self.assertEqual(len(artifact_index["artifacts"]), 1)
        self.assertEqual(artifact_index["artifacts"][0]["version"], "v1")

        manifest_index = json.loads(payloads[generated.MANIFESTS_FILENAME])
        self.assertEqual(
            [item["playgroundId"] for item in manifest_index["manifests"]],
            [public_id],
        )
        self.assertNotIn(
            private_commit, payloads[generated.MANIFESTS_FILENAME].decode()
        )

        bootstrap = json.loads(payloads[generated.BOOTSTRAP_FILENAME])
        self.assertEqual(bootstrap["catalog"], catalog["catalog"])
        self.assertEqual(
            bootstrap["artifacts"], artifact_index["artifacts"]
        )

    def test_rebuild_is_deterministic(self):
        self.publish(f"public-{uuid.uuid4().hex[:8]}", playgrounds.PUBLIC_POLICY)
        self.write_artifact("v1", "runtime.exe", b"runtime")

        first = self.rebuild()
        second = self.rebuild()

        self.assertEqual(first, second)
        self.assertEqual(self.read_generated(), first)

    def test_delete_all_generated_files_then_rebuild_restores_them(self):
        self.publish(f"public-{uuid.uuid4().hex[:8]}", playgrounds.PUBLIC_POLICY)
        self.write_artifact("v1", "runtime.exe", b"runtime")
        first = self.rebuild()

        for filename in generated.GENERATED_FILENAMES:
            (self.generated_path / filename).unlink()

        self.assertEqual(self.read_generated(), {})
        self.assertEqual(
            list(self.generated_path.iterdir())
            if self.generated_path.exists()
            else [],
            [],
        )
        second = self.rebuild()
        self.assertEqual(first, second)
        self.assertEqual(self.read_generated(), second)

    def test_failure_before_replacement_keeps_old_output(self):
        self.publish(f"public-{uuid.uuid4().hex[:8]}", playgrounds.PUBLIC_POLICY)
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

    def test_private_authorization_does_not_use_generated_cache(self):
        private_id = f"private-{uuid.uuid4().hex[:8]}"
        _, private_commit = self.publish(
            private_id, playgrounds.RELEASE_ONLY_POLICY
        )
        self.rebuild()

        # Forge the cache to advertise the private commit.  Authorization is
        # live and must still deny an unauthenticated read.
        forged = {
            "catalog": [
                {"playgroundId": private_id, "commitId": private_commit}
            ]
        }
        generated.atomic_write(
            self.generated_path / generated.CATALOG_FILENAME,
            json.dumps(forged, sort_keys=True).encode("utf-8"),
        )

        with self.assertRaises(authorization.ContentAccessDeniedError):
            manifests.generate_manifest(
                self.connection,
                None,
                self.repository,
                private_id,
                private_commit,
            )

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
