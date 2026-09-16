"""Tests for the managed Server filesystem permission policy."""

import os
import stat
import tempfile
import unittest
from pathlib import Path

import permissions
from database import open_database
import repository


class PermissionPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.base = Path(self.temporary_directory.name)

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_fresh_directory_and_file_modes(self):
        target = self.base / "data"
        permissions.ensure_private_directory(target)
        self.assertEqual(self.mode(target), 0o700)

        database = target / "app.db"
        database.write_bytes(b"x")
        permissions.ensure_private_file(database)
        self.assertEqual(self.mode(database), 0o600)

    def test_permissive_modes_are_narrowed(self):
        target = self.base / "data"
        target.mkdir()
        os.chmod(target, 0o755)
        permissions.ensure_private_directory(target)
        self.assertEqual(self.mode(target), 0o700)

        database = target / "app.db"
        database.write_bytes(b"x")
        os.chmod(database, 0o644)
        permissions.ensure_private_file(database)
        self.assertEqual(self.mode(database), 0o600)

    def test_stricter_modes_are_not_broadened(self):
        target = self.base / "data"
        target.mkdir()
        os.chmod(target, 0o500)
        permissions.ensure_private_directory(target)
        self.assertEqual(self.mode(target), 0o500)

    def test_symlinks_and_wrong_types_are_rejected(self):
        real = self.base / "real"
        real.mkdir()
        link = self.base / "link"
        link.symlink_to(real)
        with self.assertRaises(permissions.PermissionPolicyError):
            permissions.ensure_private_directory(link)

        regular = self.base / "regular"
        regular.write_bytes(b"x")
        with self.assertRaises(permissions.PermissionPolicyError):
            permissions.ensure_private_directory(regular)
        with self.assertRaises(permissions.PermissionPolicyError):
            permissions.ensure_private_file(real)

    def test_open_database_creates_private_files(self):
        database = self.base / "data" / "app.db"
        connection = open_database(database)
        connection.close()
        self.assertEqual(self.mode(database), 0o600)
        self.assertEqual(self.mode(database.parent), 0o700)

    def test_open_repository_creates_private_directory(self):
        path = self.base / "data" / "picopg.git"
        repository.open_repository(path)
        self.assertEqual(self.mode(path), 0o700)

    def test_systemd_unit_declares_each_hardening_directive_once(self):
        unit = (
            Path(__file__).resolve().parents[2] / "deploy" / "picopg.service"
        )
        text = unit.read_text(encoding="utf-8")
        for directive in (
            "UMask=0077",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            "ReadWritePaths=%h/picopg/data",
            "RestrictSUIDSGID=true",
            "LockPersonality=true",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        ):
            with self.subTest(directive=directive):
                self.assertEqual(text.count(directive), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
