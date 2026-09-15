"""Tests for account operations."""

import tempfile
import unittest
from pathlib import Path

from accounts import MAX_USERNAME_LENGTH, UsernameTakenError, register_student
from database import open_database
from passwords import verify_password


class AccountTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        database_path = Path(self.temporary_directory.name) / "app.db"
        self.connection = open_database(database_path)
        self.addCleanup(self.connection.close)

    def test_register_student(self):
        user = register_student(self.connection, "  alice  ", "secret")

        row = self.connection.execute(
            "SELECT * FROM users WHERE id = ?", (user.id,)
        ).fetchone()
        self.assertEqual(user.username, "alice")
        self.assertEqual(user.role, "student")
        self.assertEqual(row["username"], user.username)
        self.assertEqual(row["role"], "student")
        self.assertTrue(verify_password("secret", row["password_hash"]))
        self.assertNotIn("password", vars(user))

    def test_registration_rejects_invalid_input(self):
        cases = (
            ("", "secret"),
            (" " * 3, "secret"),
            ("a" * (MAX_USERNAME_LENGTH + 1), "secret"),
            ("alice", ""),
        )

        for username, password in cases:
            with self.subTest(username=username, password=password):
                with self.assertRaises(ValueError):
                    register_student(self.connection, username, password)

        count = self.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        self.assertEqual(count, 0)

    def test_registration_rejects_duplicate_username(self):
        original = register_student(self.connection, "alice", "first password")

        with self.assertRaises(UsernameTakenError):
            register_student(self.connection, "alice", "second password")

        rows = self.connection.execute("SELECT * FROM users").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], original.id)
        self.assertTrue(verify_password("first password", rows[0]["password_hash"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
