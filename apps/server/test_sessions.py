"""Tests for account session lifecycle."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from accounts import register_student
from database import open_database
from sessions import (
    SESSION_LIFETIME,
    InvalidCredentialsError,
    InvalidSessionError,
    authenticate_session,
    hash_session_token,
    login,
    revoke_session,
)


NOW = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        database_path = Path(self.temporary_directory.name) / "app.db"
        self.connection = open_database(database_path)
        self.addCleanup(self.connection.close)
        self.user = register_student(self.connection, "alice", "secret")

    def test_login_returns_token_but_stores_only_hash(self):
        session = login(self.connection, "alice", "secret", now=NOW)

        row = self.connection.execute("SELECT * FROM sessions").fetchone()
        self.assertEqual(row["token_hash"], hash_session_token(session.token))
        self.assertNotEqual(row["token_hash"], session.token)
        self.assertEqual(session.user, self.user)
        self.assertEqual(
            session.expires_at,
            (NOW + SESSION_LIFETIME)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        )

    def test_login_rejects_unknown_user_and_wrong_password(self):
        cases = (("missing", "secret"), ("alice", "wrong"))

        for username, password in cases:
            with self.subTest(username=username):
                with self.assertRaises(InvalidCredentialsError):
                    login(self.connection, username, password, now=NOW)

        count = self.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(count, 0)

    def test_authentication_checks_expiry(self):
        session = login(self.connection, "alice", "secret", now=NOW)

        authenticated = authenticate_session(
            self.connection,
            session.token,
            now=NOW + SESSION_LIFETIME - timedelta(microseconds=1),
        )
        self.assertEqual(authenticated, self.user)

        with self.assertRaises(InvalidSessionError):
            authenticate_session(
                self.connection,
                session.token,
                now=NOW + SESSION_LIFETIME,
            )

    def test_revoke_is_idempotent_and_blocks_authentication(self):
        session = login(self.connection, "alice", "secret", now=NOW)

        self.assertTrue(revoke_session(self.connection, session.token, now=NOW))
        self.assertFalse(revoke_session(self.connection, session.token, now=NOW))

        with self.assertRaises(InvalidSessionError):
            authenticate_session(self.connection, session.token, now=NOW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
