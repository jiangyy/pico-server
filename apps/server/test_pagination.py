"""Tests for the opaque, route-bound pagination cursors."""

import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pagination  # noqa: E402


class CursorTest(unittest.TestCase):
    def test_round_trip_and_route_binding(self):
        route = pagination.SUBMISSIONS_ROUTE
        cursor = pagination.encode_cursor(route, "row-1")
        self.assertEqual(pagination.decode_cursor(cursor, route), "row-1")
        # A cursor is bound to the route that minted it, so one route can
        # never accept another route's cursor.
        with self.assertRaises(pagination.PaginationError):
            pagination.decode_cursor(cursor, "another-route")

    def test_cursor_is_canonical_base64url_json(self):
        cursor = pagination.encode_cursor(
            pagination.SUBMISSIONS_ROUTE, "user-1"
        )
        self.assertNotIn("=", cursor)
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(
            payload,
            {
                "key": "user-1",
                "route": pagination.SUBMISSIONS_ROUTE,
                "v": pagination.CURSOR_VERSION,
            },
        )

    def test_non_canonical_or_malformed_cursors_are_rejected(self):
        route = pagination.SUBMISSIONS_ROUTE
        canonical = pagination.encode_cursor(route, "user-1")

        def encoded(payload):
            data = json.dumps(payload).encode("utf-8")
            return (
                base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")
            )

        variants = [
            "",
            "!!not-base64!!",
            canonical + "=",
            encoded("not json"),
            encoded({"key": "k", "route": route, "v": 99}),
            encoded({"key": "k", "route": route, "v": 1, "extra": 1}),
            encoded({"key": "", "route": route, "v": 1}),
            encoded({"key": "k", "route": "other.route", "v": 1}),
        ]
        for cursor in variants:
            with self.subTest(cursor=cursor):
                with self.assertRaises(pagination.PaginationError):
                    pagination.decode_cursor(cursor, route)


class ParsePaginationTest(unittest.TestCase):
    ROUTE = pagination.SUBMISSIONS_ROUTE

    def test_default_and_explicit_limit(self):
        self.assertEqual(
            pagination.parse_pagination("", self.ROUTE),
            (pagination.DEFAULT_LIMIT, None),
        )
        self.assertEqual(
            pagination.parse_pagination("limit=1", self.ROUTE), (1, None)
        )
        self.assertEqual(
            pagination.parse_pagination("limit=100", self.ROUTE),
            (100, None),
        )

    def test_cursor_is_decoded_to_its_key(self):
        cursor = pagination.encode_cursor(self.ROUTE, "user-7")
        self.assertEqual(
            pagination.parse_pagination(
                f"limit=5&cursor={cursor}", self.ROUTE
            ),
            (5, "user-7"),
        )

    def test_malformed_queries_are_rejected(self):
        for query in (
            "limit=0",
            "limit=101",
            "limit=",
            "limit=+1",
            "limit= 1",
            "limit=1.0",
            "limit=-1",
            "limit=999999999999999999999999",
            "limit=\u0661",
            "unknown=1",
            "limit=1&limit=2",
            "cursor=",
            "cursor=not-a-cursor",
        ):
            with self.subTest(query=query):
                with self.assertRaises(pagination.PaginationError):
                    pagination.parse_pagination(query, self.ROUTE)

    def test_cursor_from_another_route_is_rejected(self):
        cursor = pagination.encode_cursor("another-route", "pg-1")
        with self.assertRaises(pagination.PaginationError):
            pagination.parse_pagination(f"cursor={cursor}", self.ROUTE)


    def test_page_is_sequence_like(self):
        page = pagination.Page(items=("a", "b"), next_key=["b"])
        self.assertEqual(len(page), 2)
        self.assertEqual(list(page), ["a", "b"])
        self.assertEqual(page[1], "b")
        self.assertEqual(page, ["a", "b"])
        self.assertEqual(page.next_key, ["b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
