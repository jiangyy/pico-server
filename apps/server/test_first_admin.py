"""Tests for the one-shot first-Admin bootstrap.

An Admin is the only role that can create other managed accounts, so a fresh
data directory has a chicken-and-egg problem: no HTTP route can mint the first
one.  ``maintenance.py create-admin`` is the offline answer, and these tests
pin the two properties that make it safe to keep around:

1. It creates exactly one Admin on a fresh database.
2. It refuses once any Admin exists, so it never becomes a second door.
"""

import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import accounts
import maintenance
from database import open_database
from server import make_server


class FirstAdminTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.data_dir = Path(self.temporary_directory.name) / "data"
        self.data_dir.mkdir()
        # The Server owns data-directory initialization; the CLI must run
        # against a directory the Server has already created.
        self.server = make_server(
            host="127.0.0.1",
            port=0,
            database_path=self.data_dir / "app.db",
            repository_path=self.data_dir / "picopg.git",
            audit_output=io.StringIO(),
        )
        self.server.server_close()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _run_cli(self, *argv):
        buffer = io.StringIO()
        import contextlib

        with contextlib.redirect_stdout(buffer):
            code = maintenance._run_cli(list(argv))
        return code, json.loads(buffer.getvalue().strip())

    def test_creates_the_first_admin_and_only_one(self):
        code, summary = self._run_cli(
            "create-admin",
            "--data-dir", str(self.data_dir),
            "--username", "root-admin",
            "--password", "correct-horse-battery",
        )
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["summary"]["username"], "root-admin")
        self.assertEqual(summary["summary"]["role"], "admin")

        code, failure = self._run_cli(
            "create-admin",
            "--data-dir", str(self.data_dir),
            "--username", "second-admin",
            "--password", "another-password",
        )
        self.assertEqual(code, 1)
        self.assertEqual(failure["error"], maintenance.REASON_ADMIN_EXISTS)

        # The refused call left the table untouched.
        connection = open_database(self.data_dir / "app.db")
        try:
            usernames = [
                row["username"]
                for row in connection.execute(
                    "SELECT username FROM users ORDER BY username"
                )
            ]
        finally:
            connection.close()
        self.assertEqual(usernames, ["root-admin"])

    def test_refuses_a_data_directory_without_a_database(self):
        empty = Path(self.temporary_directory.name) / "empty"
        empty.mkdir()
        code, failure = self._run_cli(
            "create-admin",
            "--data-dir", str(empty),
            "--username", "root-admin",
            "--password", "correct-horse-battery",
        )
        self.assertEqual(code, 1)
        self.assertEqual(failure["error"], maintenance.REASON_DATABASE_INVALID)

    def test_a_student_account_does_not_block_the_bootstrap(self):
        # A student may be registered through the public API before the
        # operator creates the first Admin; only an existing *Admin* blocks.
        connection = open_database(self.data_dir / "app.db")
        try:
            with connection:
                accounts.register_student(connection, "early-student", "pw")
        finally:
            connection.close()

        code, summary = self._run_cli(
            "create-admin",
            "--data-dir", str(self.data_dir),
            "--username", "root-admin",
            "--password", "correct-horse-battery",
        )
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["summary"]["role"], "admin")


if __name__ == "__main__":
    unittest.main()
