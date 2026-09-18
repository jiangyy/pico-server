"""Opaque, route-bound keyset pagination cursors.

A cursor is the base64url encoding (without padding) of one canonical JSON
object::

    {"key": "<value>" | ["<value>", ...], "route": "<route name>", "v": 1}

The decoded fields are validated exactly, and the cursor is bound to one route
name and one format version.  A cursor from another route or another parent
filter is therefore rejected: filtered routes embed the parent id in the route
name (for example ``class.releases:<classId>``).  Untrusted cursor text only
ever becomes a bound SQL parameter; it is never interpolated into SQL.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from urllib.parse import parse_qs


__all__ = [
    "CATALOG_ROUTE",
    "CURSOR_VERSION",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "Page",
    "PaginationError",
    "SUBMISSIONS_ROUTE",
    "decode_cursor",
    "encode_cursor",
    "parse_pagination",
    "scoped_route",
]


CURSOR_VERSION = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 100

SUBMISSIONS_ROUTE = "submissions"
CATALOG_ROUTE = "catalog"

_BASE64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


class PaginationError(ValueError):
    """A list query or cursor is malformed or bound to another route."""


@dataclass(frozen=True, eq=False)
class Page:
    """One keyset page: the visible items and the exclusive next key.

    The page is sequence-like so existing callers can iterate, index, compare
    to a list, or take its length without unwrapping ``items`` by hand.
    """

    items: tuple
    next_key: object | None

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __getitem__(self, index):
        return self.items[index]

    def __eq__(self, other):
        if isinstance(other, Page):
            return (
                self.items == other.items
                and self.next_key == other.next_key
            )
        if isinstance(other, (list, tuple)):
            return list(self.items) == list(other)
        return NotImplemented

    def __hash__(self):
        return hash((self.items, tuple(self.next_key) if isinstance(self.next_key, list) else self.next_key))


def scoped_route(base, parent_id):
    """Return the route name for one parent-filtered list endpoint."""

    return f"{base}:{parent_id}"


def _canonical_key(key):
    """Return the canonical cursor key, or raise ``PaginationError``."""

    if type(key) is str:
        if not key:
            raise PaginationError("cursor key is empty")
        return key
    if type(key) in (list, tuple):
        values = list(key)
        if not values:
            raise PaginationError("cursor key is empty")
        for value in values:
            if type(value) is not str or not value:
                raise PaginationError("cursor key field is invalid")
        return values
    raise PaginationError("cursor key has an unsupported type")


def _canonical_payload(route, key):
    return json.dumps(
        {"key": _canonical_key(key), "route": route, "v": CURSOR_VERSION},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def encode_cursor(route, key):
    """Return the opaque cursor for ``route`` after the row ``key``."""

    encoded = base64.urlsafe_b64encode(
        _canonical_payload(route, key)
    ).decode("ascii")
    return encoded.rstrip("=")


def decode_cursor(cursor, route):
    """Return the last cursor key from ``cursor``, or raise ``PaginationError``."""

    if type(cursor) is not str or not cursor:
        raise PaginationError("cursor must be a non-empty string")
    if any(character not in _BASE64URL_ALPHABET for character in cursor):
        raise PaginationError("cursor is not base64url")
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding)
    except (binascii.Error, ValueError) as error:
        raise PaginationError("cursor is not valid base64url") from error
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise PaginationError("cursor is not valid JSON") from error

    if type(payload) is not dict or set(payload) != {"key", "route", "v"}:
        raise PaginationError("cursor has an unexpected shape")
    if payload["v"] != CURSOR_VERSION:
        raise PaginationError("cursor version is not supported")
    if payload["route"] != route:
        raise PaginationError("cursor is bound to another route")
    key = _canonical_key(payload.get("key"))
    if encode_cursor(route, key) != cursor:
        raise PaginationError("cursor is not canonical")
    return key


def parse_pagination(query, route):
    """Return ``(limit, after_key)`` for one list route.

    Only ``limit`` and ``cursor`` are accepted.  ``after_key`` is ``None`` for
    a first page.
    """

    pairs = parse_qs(query, keep_blank_values=True)
    if set(pairs) - {"limit", "cursor"}:
        raise PaginationError("unknown query parameter")
    for values in pairs.values():
        if len(values) != 1:
            raise PaginationError("duplicate query parameter")

    limit = DEFAULT_LIMIT
    if "limit" in pairs:
        raw = pairs["limit"][0]
        if not raw.isascii() or not raw.isdigit():
            raise PaginationError("limit must be decimal ASCII")
        limit = int(raw)
        if not 1 <= limit <= MAX_LIMIT:
            raise PaginationError("limit is out of range")

    after = None
    if "cursor" in pairs:
        after = decode_cursor(pairs["cursor"][0], route)
    return limit, after
