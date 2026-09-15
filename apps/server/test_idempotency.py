"""Tests for persistent transactional idempotency."""

import json
import tempfile
import unittest
from pathlib import Path

from database import open_database
from idempotency import (
    IdempotencyConflict,
    IdempotencyRequestError,
    MAX_ENDPOINT_LENGTH,
    MAX_REQUEST_BYTES,
    MAX_REQUEST_KEY_LENGTH,
    canonicalize_request,
    execute,
    request_digest,
)


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "app.db"

    def open(self):
        connection = open_database(self.database_path)
        self.addCleanup(connection.close)
        return connection

    def create_user(self, connection, user_id="actor-1", username="actor"):
        connection.execute(
            """
            INSERT INTO users (id, username, role, password_hash, created_at)
            VALUES (?, ?, 'teacher', 'hash', '2026-09-15T00:00:00Z')
            """,
            (user_id, username),
        )

    def count(self, connection, table):
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def count_business_user(self, connection, user_id):
        return connection.execute(
            "SELECT COUNT(*) FROM users WHERE id = ?", (user_id,)
        ).fetchone()[0]

    def test_exact_retry_returns_stored_result_without_callback(self):
        connection = self.open()
        self.create_user(connection)
        connection.commit()
        calls = []

        def callback(conn):
            calls.append(1)
            return 201, {"id": "playground-1", "nested": {"b": 2, "a": 1}}

        first = execute(
            connection,
            "actor-1",
            "/playgrounds",
            "key-1",
            {"name": "A", "policy": "release_only"},
            callback,
        )
        self.assertEqual(
            first, (201, {"id": "playground-1", "nested": {"b": 2, "a": 1}})
        )
        self.assertEqual(calls, [1])

        def forbidden_callback(conn):
            raise AssertionError("callback must not run on an exact retry")

        second = execute(
            connection,
            "actor-1",
            "/playgrounds",
            "key-1",
            {"policy": "release_only", "name": "A"},
            forbidden_callback,
        )
        self.assertEqual(second, first)
        self.assertEqual(calls, [1])
        self.assertEqual(self.count(connection, "idempotency_results"), 1)

    def test_same_key_with_different_request_conflicts(self):
        connection = self.open()
        self.create_user(connection)
        connection.commit()
        calls = []

        def callback(conn):
            calls.append(1)
            return 201, {"id": "playground-1"}

        execute(
            connection, "actor-1", "/playgrounds", "key-1",
            {"name": "A"}, callback,
        )

        with self.assertRaises(IdempotencyConflict):
            execute(
                connection, "actor-1", "/playgrounds", "key-1",
                {"name": "B"}, callback,
            )
        self.assertEqual(calls, [1])
        self.assertEqual(self.count(connection, "idempotency_results"), 1)

    def test_actor_scope_is_isolated(self):
        connection = self.open()
        self.create_user(connection, user_id="actor-1", username="actor-1")
        self.create_user(connection, user_id="actor-2", username="actor-2")
        connection.commit()

        first = execute(
            connection, "actor-1", "/playgrounds", "key-1",
            {"name": "A"}, lambda conn: (201, {"actor": "actor-1"}),
        )
        second = execute(
            connection, "actor-2", "/playgrounds", "key-1",
            {"name": "B"}, lambda conn: (201, {"actor": "actor-2"}),
        )

        self.assertEqual(first, (201, {"actor": "actor-1"}))
        self.assertEqual(second, (201, {"actor": "actor-2"}))
        self.assertEqual(self.count(connection, "idempotency_results"), 2)

    def test_endpoint_scope_is_isolated(self):
        connection = self.open()
        self.create_user(connection)
        connection.commit()

        first = execute(
            connection, "actor-1", "/playgrounds", "key-1",
            {"name": "A"}, lambda conn: (201, {"endpoint": "playgrounds"}),
        )
        second = execute(
            connection, "actor-1", "/submissions", "key-1",
            {"name": "B"}, lambda conn: (201, {"endpoint": "submissions"}),
        )

        self.assertEqual(first, (201, {"endpoint": "playgrounds"}))
        self.assertEqual(second, (201, {"endpoint": "submissions"}))
        self.assertEqual(self.count(connection, "idempotency_results"), 2)

    def test_success_commits_business_row_and_result_together(self):
        connection = self.open()
        self.create_user(connection)
        connection.commit()

        def callback(conn):
            self.create_user(conn, user_id="business-1", username="business")
            return 201, {"id": "business-1"}

        result = execute(
            connection, "actor-1", "/playgrounds", "key-1",
            {"name": "A"}, callback,
        )

        self.assertEqual(result, (201, {"id": "business-1"}))
        self.assertEqual(self.count_business_user(connection, "business-1"), 1)
        self.assertEqual(self.count(connection, "idempotency_results"), 1)

    def test_callback_failure_rolls_back_business_and_result(self):
        connection = self.open()
        self.create_user(connection)
        connection.commit()

        def callback(conn):
            self.create_user(conn, user_id="business-1", username="business")
            raise RuntimeError("business failure")

        with self.assertRaises(RuntimeError):
            execute(
                connection, "actor-1", "/playgrounds", "key-1",
                {"name": "A"}, callback,
            )

        self.assertEqual(self.count_business_user(connection, "business-1"), 0)
        self.assertEqual(self.count(connection, "idempotency_results"), 0)

    def test_failed_attempt_does_not_poison_retry(self):
        connection = self.open()
        self.create_user(connection)
        connection.commit()

        def failing(conn):
            raise RuntimeError("first attempt fails")

        with self.assertRaises(RuntimeError):
            execute(
                connection, "actor-1", "/playgrounds", "key-1",
                {"name": "A"}, failing,
            )

        result = execute(
            connection, "actor-1", "/playgrounds", "key-1",
            {"name": "A"}, lambda conn: (201, {"id": "playground-1"}),
        )
        self.assertEqual(result, (201, {"id": "playground-1"}))
        self.assertEqual(self.count(connection, "idempotency_results"), 1)

    def test_invalid_callback_result_rolls_back_business_row(self):
        connection = self.open()
        self.create_user(connection)
        connection.commit()

        def callback(conn):
            self.create_user(conn, user_id="business-1", username="business")
            return 201, {"bad": object()}

        with self.assertRaises(IdempotencyRequestError):
            execute(
                connection, "actor-1", "/playgrounds", "key-1",
                {"name": "A"}, callback,
            )

        self.assertEqual(self.count_business_user(connection, "business-1"), 0)
        self.assertEqual(self.count(connection, "idempotency_results"), 0)

    def test_empty_and_oversized_scope_is_rejected_before_work(self):
        connection = self.open()
        self.create_user(connection)
        connection.commit()
        calls = []

        def callback(conn):
            calls.append(1)
            return 201, {"id": "playground-1"}

        invalid_scopes = (
            ("", "key-1"),
            ("/playgrounds", ""),
            ("e" * (MAX_ENDPOINT_LENGTH + 1), "key-1"),
            ("/playgrounds", "k" * (MAX_REQUEST_KEY_LENGTH + 1)),
        )
        for endpoint, request_key in invalid_scopes:
            with self.assertRaises(IdempotencyRequestError):
                execute(
                    connection, "actor-1", endpoint, request_key,
                    {"name": "A"}, callback,
                )

        self.assertEqual(calls, [])
        self.assertEqual(self.count(connection, "idempotency_results"), 0)

    def test_scope_at_maximum_length_is_accepted(self):
        connection = self.open()
        self.create_user(connection)
        connection.commit()

        result = execute(
            connection,
            "actor-1",
            "e" * MAX_ENDPOINT_LENGTH,
            "k" * MAX_REQUEST_KEY_LENGTH,
            {"name": "A"},
            lambda conn: (201, {"id": "playground-1"}),
        )

        self.assertEqual(result, (201, {"id": "playground-1"}))

    def test_oversized_request_is_rejected_before_callback(self):
        connection = self.open()
        self.create_user(connection)
        connection.commit()
        calls = []

        def callback(conn):
            calls.append(1)
            return 201, {"id": "playground-1"}

        oversized = {"data": "x" * (MAX_REQUEST_BYTES + 1)}
        with self.assertRaises(IdempotencyRequestError):
            execute(
                connection, "actor-1", "/playgrounds", "key-1",
                oversized, callback,
            )

        self.assertEqual(calls, [])
        self.assertEqual(self.count(connection, "idempotency_results"), 0)

    def test_request_must_be_json_serializable(self):
        with self.assertRaises(IdempotencyRequestError):
            canonicalize_request({"value": object()})

    def test_canonical_request_is_key_order_independent(self):
        first = canonicalize_request(
            {"a": 1, "b": [1, 2], "c": {"y": 2, "x": 1}}
        )
        second = canonicalize_request(
            {"c": {"x": 1, "y": 2}, "b": [1, 2], "a": 1}
        )

        self.assertEqual(first, second)
        self.assertEqual(
            request_digest({"a": 1, "b": 2}),
            request_digest({"b": 2, "a": 1}),
        )

    def test_stored_response_is_json_without_secret_or_raw_body(self):
        connection = self.open()
        self.create_user(connection)
        connection.commit()

        execute(
            connection,
            "actor-1",
            "/playgrounds",
            "key-1",
            {"name": "Lesson", "authorization": "Bearer super-secret-token"},
            lambda conn: (201, {"id": "playground-1"}),
        )

        row = connection.execute(
            """
            SELECT request_sha256, response_json, response_status, created_at
            FROM idempotency_results
            """
        ).fetchone()
        self.assertEqual(json.loads(row["response_json"]), {"id": "playground-1"})
        self.assertEqual(row["response_status"], 201)
        self.assertEqual(len(row["request_sha256"]), 64)
        self.assertTrue(row["created_at"])

        stored = " ".join(str(value) for value in tuple(row))
        self.assertNotIn("super-secret-token", stored)
        self.assertNotIn("Bearer", stored)
        self.assertNotIn("Lesson", stored)
        self.assertNotIn("authorization", stored)

    def test_concurrent_attempt_resolves_through_unique_constraint(self):
        connection = self.open()
        other = self.open()
        self.create_user(connection)
        connection.commit()

        def winner(conn):
            return 201, {"id": "winner"}

        def loser(conn):
            # Simulate a competing request committing after this attempt's
            # initial lookup but before it records its own result.
            execute(
                other, "actor-1", "/playgrounds", "key-1",
                {"name": "A"}, winner,
            )
            self.create_user(conn, user_id="business-1", username="business")
            return 500, {"id": "loser"}

        result = execute(
            connection, "actor-1", "/playgrounds", "key-1",
            {"name": "A"}, loser,
        )

        self.assertEqual(result, (201, {"id": "winner"}))
        self.assertEqual(self.count_business_user(connection, "business-1"), 0)
        self.assertEqual(self.count(connection, "idempotency_results"), 1)
        stored = connection.execute(
            "SELECT response_json FROM idempotency_results"
        ).fetchone()["response_json"]
        self.assertEqual(json.loads(stored), {"id": "winner"})

    def test_concurrent_attempt_with_different_request_conflicts(self):
        connection = self.open()
        other = self.open()
        self.create_user(connection)
        connection.commit()

        def loser(conn):
            execute(
                other, "actor-1", "/playgrounds", "key-1",
                {"name": "A"}, lambda inner: (201, {"id": "winner"}),
            )
            return 201, {"id": "loser"}

        with self.assertRaises(IdempotencyConflict):
            execute(
                connection, "actor-1", "/playgrounds", "key-1",
                {"name": "B"}, loser,
            )

        self.assertEqual(self.count(connection, "idempotency_results"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
