"""Opaque, route-bound keyset pagination cursors.

A cursor is the base64url encoding (without padding) of one canonical JSON
object::

    {"key": "<last sort key>", "route": "<route name>", "v": 1}

The decoded fields are validated exactly, and the cursor is bound to one route
name and one format version.  A cursor from another route, a non-canonical
encoding, an extra field, or a changed version is rejected instead of being
interpreted.  Untrusted cursor text only ever becomes a bound SQL parameter;
it is never interpolated into SQL.
"""

import base64
import binascii
import json
from urllib.parse import parse_qs


__all__ = [
    "ADMIN_PLAYGROUNDS_ROUTE",
    "ADMIN_USERS_ROUTE",
    "CURSOR_VERSION",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "PaginationError",
    "decode_cursor",
    "encode_cursor",
    "parse_pagination",
]


CURSOR_VERSION = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 100

ADMIN_USERS_ROUTE = "admin.users"
ADMIN_PLAYGROUNDS_ROUTE = "admin.playgrounds"

_BASE64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


class PaginationError(ValueError):
    """A list query or cursor is malformed or bound to another route."""


def _canonical_payload(route, key):
    return json.dumps(
        {"key": key, "route": route, "v": CURSOR_VERSION},
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
    """Return the last sort key from ``cursor``, or raise ``PaginationError``."""

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
    key = payload["key"]
    if type(key) is not str or not key:
        raise PaginationError("cursor key is not a non-empty string")
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
