"""Tests for the offline backup and restore tooling.

The tests build a realistic data directory (SQLite database, a real bare Git
repository with a published Playground and a Student Work commit, artifacts
and a disposable generated cache), then exercise a full backup/validate/
restore round trip plus the failure paths that must never produce a partial
or overwritten recovery set.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

# Allow ``import maintenance`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import accounts  # noqa: E402
import artifacts  # noqa: E402
import bundles  # noqa: E402
import classes  # noqa: E402
import generated  # noqa: E402
import maintenance  # noqa: E402
import manifests  # noqa: E402
import playgrounds  # noqa: E402
import releases  # noqa: E402
import repository  # noqa: E402
import submissions  # noqa: E402
import works  # noqa: E402
from database import open_database  # noqa: E402


MANIFEST_BYTES = b'{"schemaVersion":1,"runtime":"python"}'
OPEN_PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.base = Path(self.temporary_directory.name)

        self.data_dir = self.base / "data"
        self.data_dir.mkdir()
        self.database_path = self.data_dir / "app.db"
        self.connection = open_database(self.database_path)
        self.addCleanup(self.connection.close)
        self.repository_path = self.data_dir / "picopg.git"
        self.repository = repository.open_repository(self.repository_path)
        self.artifacts_path = self.data_dir / "artifacts"
        self.generated_path = self.data_dir / "generated"
        self.backup_path = self.base / "backup"

        self.build_dataset()

    # ----- fixtures -----------------------------------------------------

    def build_dataset(self):
        bootstrap = accounts.User(
            id="seed-actor",
            username="seed-actor",
            role="admin",
            created_at="2026-01-01T00:00:00Z",
        )
        self.admin = accounts.create_user(
            self.connection, bootstrap, "admin", "secret", "admin"
        )
        self.teacher = accounts.create_user(
            self.connection, self.admin, "teacher", "secret", "teacher"
        )
        self.student = accounts.create_user(
            self.connection, self.admin, "student", "secret", "student"
        )
        self.klass = classes.create_class(
            self.connection, self.admin, "Class", self.teacher.id
        )
        classes.assign_student(
            self.connection, self.admin, self.student.id, self.klass.id
        )

        self.playground_id = "pg-maintenance"
        playgrounds.create_playground(
            self.connection, self.admin, self.playground_id
        )
        playgrounds.update_playground(
            self.connection,
            self.admin,
            self.playground_id,
            {"policy": playgrounds.PUBLIC_POLICY},
        )
        bundle = bundles.validate_bundle(
            [
                bundles.BundleEntry("picopg.json", MANIFEST_BYTES),
                bundles.BundleEntry(
                    "course/module/app.py", b"print('hello')\n"
                ),
            ]
        )
        published = playgrounds.publish_playground(
            self.connection,
            self.admin,
            self.playground_id,
            self.repository,
            bundle,
            repository.CommitMetadata(
                name="Test Author",
                email="author@example.test",
                timestamp="2026-01-01T00:00:00+00:00",
                message="publish",
            ),
        )
        self.playground_commit_id = published.commit_id

        self.artifact_data = b"runtime-binary"
        version_dir = self.artifacts_path / "v1"
        version_dir.mkdir(parents=True)
        (version_dir / "runtime.exe").write_bytes(self.artifact_data)

        self.generated_path.mkdir()
        (self.generated_path / "catalog.json").write_bytes(b"stale cache")

        status, body = releases.create_release(
            self.connection,
            self.teacher,
            self.repository,
            self.klass.id,
            self.playground_id,
            self.playground_commit_id,
            OPEN_PAST,
            False,
            "release-key",
        )
        self.assertEqual(status, 201)
        self.release_id = body["release"]["id"]

        work = works.create_work_commit(
            self.connection,
            self.student,
            self.repository,
            self.playground_id,
            self.playground_commit_id,
            works.validate_work_files(
                [works.WorkEntry("work/main.py", b"print('work')\n")]
            ),
        )
        self.work_commit_id = work.commit_id

        status, body = submissions.create_submission(
            self.connection,
            self.student,
            self.repository,
            self.release_id,
            self.work_commit_id,
            "submission-key",
        )
        self.assertEqual(status, 201)

    def make_backup(self):
        return maintenance.create_backup(
            self.data_dir, self.backup_path, service_stopped=True
        )

    def rewrite_manifest(self, backup):
        entries = maintenance._build_manifest(backup)
        maintenance._write_manifest(backup, entries)

    def manifest_paths(self):
        mapping = json.loads(
            (self.backup_path / maintenance.MANIFEST_FILENAME).read_text(
                encoding="utf-8"
            )
        )
        return {entry["path"] for entry in mapping["entries"]}

    # ----- round trip ---------------------------------------------------

    def test_backup_validate_and_restore_round_trip(self):
        created = self.make_backup()
        self.assertEqual(created, self.backup_path)

        mapping = maintenance.validate_backup(self.backup_path)
        self.assertEqual(mapping["schemaVersion"], 1)
        paths = self.manifest_paths()
        self.assertIn("app.db", paths)
        self.assertIn("artifacts/v1/runtime.exe", paths)
        self.assertTrue(any(path.startswith("picopg.git/") for path in paths))
        self.assertFalse(any(path.startswith("generated/") for path in paths))

        restore_dir = self.base / "restore"
        restored = maintenance.restore_backup(
            self.backup_path, restore_dir, service_stopped=True
        )
        self.assertEqual(restored, restore_dir)
        self.assertFalse((restore_dir / "generated").exists())

        connection = open_database(restore_dir / "app.db")
        self.addCleanup(connection.close)
        restored_repository = repository.open_repository(
            restore_dir / "picopg.git"
        )

        generated.rebuild(
            restore_dir / "generated",
            connection=connection,
            repository=restored_repository,
            artifacts_path=restore_dir / "artifacts",
        )
        self.assertTrue(
            (restore_dir / "generated" / "catalog.json").is_file()
        )

        # Representative reads go through the restored authority only.
        manifest = manifests.generate_manifest(
            connection,
            None,
            restored_repository,
            self.playground_id,
            self.playground_commit_id,
        )
        self.assertEqual(
            [item.path for item in manifest.files],
            ["course/module/app.py", "picopg.json"],
        )
        served = manifests.read_commit_file(
            connection,
            None,
            restored_repository,
            self.playground_id,
            self.playground_commit_id,
            "course/module/app.py",
        )
        self.assertEqual(served.data, b"print('hello')\n")

        artifact_entries = artifacts.index_artifacts(
            restore_dir / "artifacts"
        )
        self.assertEqual(len(artifact_entries), 1)
        self.assertEqual(
            artifacts.serve_artifact(
                restore_dir / "artifacts", artifact_entries[0]
            ),
            self.artifact_data,
        )

        restored_row = connection.execute(
            "SELECT work_commit_id FROM submissions"
        ).fetchone()
        self.assertEqual(restored_row["work_commit_id"], self.work_commit_id)

    # ----- stopped-service contract -------------------------------------

    def test_backup_requires_the_service_to_be_stopped(self):
        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.create_backup(self.data_dir, self.backup_path)
        self.assertEqual(
            caught.exception.reason, maintenance.REASON_SERVICE_RUNNING
        )
        self.assertFalse(self.backup_path.exists())

    def test_restore_requires_the_service_to_be_stopped(self):
        self.make_backup()
        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.restore_backup(self.backup_path, self.base / "restore")
        self.assertEqual(
            caught.exception.reason, maintenance.REASON_SERVICE_RUNNING
        )
        self.assertFalse((self.base / "restore").exists())

    # ----- destination handling -----------------------------------------

    def test_existing_destination_is_never_overwritten(self):
        self.make_backup()
        marker = self.backup_path / "marker"
        marker.write_text("keep", encoding="utf-8")

        with self.assertRaises(maintenance.MaintenanceError) as caught:
            self.make_backup()
        self.assertEqual(
            caught.exception.reason, maintenance.REASON_DESTINATION_EXISTS
        )
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_mid_copy_failure_leaves_no_completed_backup(self):
        with mock.patch.object(
            maintenance, "_copy_tree", side_effect=OSError("simulated crash")
        ):
            with self.assertRaises(OSError):
                self.make_backup()

        self.assertFalse(self.backup_path.exists())
        leftovers = [
            path
            for path in self.base.iterdir()
            if path.name.startswith(".backup.")
        ]
        self.assertEqual(leftovers, [])

    def test_destination_inside_data_dir_is_rejected(self):
        def snapshot(root):
            return sorted(
                os.path.relpath(os.path.join(current, name), root)
                for current, directories, files in os.walk(root)
                for name in directories + files
            )

        before = snapshot(self.data_dir)
        for relative in (
            "backup",
            "artifacts/v1/backup",
            "picopg.git/backup",
        ):
            destination = self.data_dir / relative
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                maintenance.create_backup(
                    self.data_dir, destination, service_stopped=True
                )
            self.assertEqual(
                caught.exception.reason,
                maintenance.REASON_DESTINATION_INVALID,
            )
            self.assertFalse(destination.exists())

        # The check runs before the temporary build directory is created, so
        # the copied roots are byte-for-byte untouched and nothing recursed.
        self.assertEqual(snapshot(self.data_dir), before)
        self.assertFalse(self.backup_path.exists())

    # ----- validation failure cases -------------------------------------

    def test_tampered_file_is_rejected(self):
        self.make_backup()
        (self.backup_path / "artifacts" / "v1" / "runtime.exe").write_bytes(
            b"tampered-binary"
        )

        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.validate_backup(self.backup_path)
        self.assertEqual(
            caught.exception.reason, maintenance.REASON_FILE_CHANGED
        )

    def test_missing_file_is_rejected(self):
        self.make_backup()
        (self.backup_path / "artifacts" / "v1" / "runtime.exe").unlink()

        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.validate_backup(self.backup_path)
        self.assertEqual(
            caught.exception.reason, maintenance.REASON_FILE_MISSING
        )

    def test_unlisted_file_is_rejected(self):
        self.make_backup()
        (self.backup_path / "artifacts" / "extra.bin").write_bytes(b"extra")

        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.validate_backup(self.backup_path)
        self.assertEqual(
            caught.exception.reason, maintenance.REASON_UNEXPECTED_FILE
        )

    def test_symbolic_link_inside_backup_is_rejected(self):
        self.make_backup()
        outside = self.base / "outside"
        outside.write_bytes(b"outside")
        (self.backup_path / "artifacts" / "link").symlink_to(outside)

        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.validate_backup(self.backup_path)
        self.assertEqual(
            caught.exception.reason, maintenance.REASON_PATH_ESCAPE
        )

    def test_incomplete_git_repository_is_rejected(self):
        self.make_backup()
        loose = self.first_loose_object()
        self.assertIsNotNone(loose)
        loose.unlink()
        self.rewrite_manifest(self.backup_path)

        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.validate_backup(self.backup_path)
        self.assertEqual(
            caught.exception.reason, maintenance.REASON_GIT_INVALID
        )

    def test_corrupted_git_blob_is_rejected(self):
        # ``git fsck --connectivity-only`` never reads blobs, so a full fsck is
        # required to notice a corrupted blob object.  The manifest is
        # rewritten so the hashes stay valid and the Git check is what fails.
        self.make_backup()
        blob = self.first_loose_blob(self.backup_path / "picopg.git")
        self.assertIsNotNone(blob)
        blob.chmod(0o644)
        blob.write_bytes(b"corrupted blob contents")
        self.rewrite_manifest(self.backup_path)

        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.validate_backup(self.backup_path)
        self.assertEqual(
            caught.exception.reason, maintenance.REASON_GIT_INVALID
        )

    def test_unresolved_database_commit_reference_is_rejected(self):
        with self.connection:
            self.connection.execute(
                "INSERT INTO submissions"
                " (id, release_id, student_id, work_commit_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    "bogus-submission",
                    self.release_id,
                    self.student.id,
                    "0" * 40,
                    "2026-01-01T00:00:00Z",
                ),
            )
        self.make_backup()

        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.validate_backup(self.backup_path)
        self.assertEqual(
            caught.exception.reason, maintenance.REASON_COMMIT_MISSING
        )

    def test_repository_with_object_alternates_is_rejected(self):
        alternates = self.repository_path / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text("/elsewhere/objects\n", encoding="utf-8")

        with self.assertRaises(maintenance.MaintenanceError) as caught:
            self.make_backup()
        self.assertEqual(
            caught.exception.reason, maintenance.REASON_ALTERNATES_PRESENT
        )
        self.assertFalse(self.backup_path.exists())

    # ----- restore target handling --------------------------------------

    def test_nonempty_restore_target_is_refused(self):
        self.make_backup()
        target = self.base / "restore"
        target.mkdir()
        marker = target / "live.txt"
        marker.write_bytes(b"live data")

        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.restore_backup(
                self.backup_path, target, service_stopped=True
            )
        self.assertEqual(
            caught.exception.reason, maintenance.REASON_RESTORE_TARGET_INVALID
        )
        self.assertEqual(marker.read_bytes(), b"live data")

    def test_restore_target_that_is_a_file_is_refused(self):
        self.make_backup()
        target = self.base / "restore-file"
        target.write_bytes(b"not a directory")

        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.restore_backup(
                self.backup_path, target, service_stopped=True
            )
        self.assertEqual(
            caught.exception.reason, maintenance.REASON_RESTORE_TARGET_INVALID
        )
        self.assertEqual(target.read_bytes(), b"not a directory")

    def test_restore_into_existing_empty_directory_is_allowed(self):
        self.make_backup()
        target = self.base / "restore"
        target.mkdir()

        maintenance.restore_backup(
            self.backup_path, target, service_stopped=True
        )
        self.assertTrue((target / "app.db").is_file())
        self.assertTrue((target / "picopg.git").is_dir())

    def test_restore_target_inside_the_backup_is_refused(self):
        self.make_backup()
        target = self.backup_path / "restore"

        with self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.restore_backup(
                self.backup_path, target, service_stopped=True
            )
        self.assertEqual(
            caught.exception.reason, maintenance.REASON_RESTORE_TARGET_INVALID
        )
        self.assertFalse(target.exists())

    # ----- helpers ------------------------------------------------------

    def first_loose_object(self):
        objects = self.backup_path / "picopg.git" / "objects"
        for directory in sorted(objects.iterdir()):
            if (
                directory.is_dir()
                and len(directory.name) == 2
                and directory.name != "pack"
            ):
                for candidate in sorted(directory.iterdir()):
                    if candidate.is_file():
                        return candidate
        return None

    def first_loose_blob(self, repository_path):
        objects = repository_path / "objects"
        for directory in sorted(objects.iterdir()):
            if (
                not directory.is_dir()
                or len(directory.name) != 2
                or directory.name == "pack"
            ):
                continue
            for candidate in sorted(directory.iterdir()):
                if not candidate.is_file():
                    continue
                object_id = directory.name + candidate.name
                completed = repository._run_git(
                    ("cat-file", "-t", object_id), cwd=repository_path
                )
                if (
                    completed.returncode == 0
                    and completed.stdout.strip() == b"blob"
                ):
                    return candidate
        return None


if __name__ == "__main__":
    unittest.main(verbosity=2)
