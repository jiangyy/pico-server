from __future__ import annotations

import tarfile
import tempfile
import unittest
from pathlib import Path

from deploy.backup import create, restore


class BackupTest(unittest.TestCase):
    def test_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            data = root / "data"
            (data / "replica").mkdir(parents=True)
            (data / "auth").mkdir()
            (data / "packages").mkdir()
            (data / "replica" / "records.json").write_text("immutable facts", encoding="utf-8")
            (data / "auth" / "sessions.json").write_text("sessions", encoding="utf-8")
            (data / "packages" / "lesson.pico").write_bytes(b"package")
            backup = root / "backups" / "snapshot.tar.gz"
            create(data, backup)
            restored = root / "restored"
            restore(backup, restored)
            assert (restored / "replica" / "records.json").read_text(encoding="utf-8") == "immutable facts"
            assert (restored / "auth" / "sessions.json").read_text(encoding="utf-8") == "sessions"
            assert (restored / "packages" / "lesson.pico").read_bytes() == b"package"

            unsafe = root / "unsafe.tar.gz"
            payload = root / "payload"
            payload.write_text("bad", encoding="utf-8")
            with tarfile.open(unsafe, "w:gz") as archive:
                archive.add(payload, arcname="../outside")
            try:
                restore(unsafe, root / "unsafe-restore")
            except ValueError:
                pass
            else:
                raise AssertionError("unsafe archive was restored")


if __name__ == "__main__":
    unittest.main()
