"""Persistent, transactional idempotency for create operations.

The module stores results in ``idempotency_results`` keyed by
``(scope, endpoint, request_key)``.  A scope is a caller-chosen name — the
operator for a publish, the student's own name for a submission — so two callers
never share a key space and no account model is implied.  A caller supplies a
logical request that is canonicalized and hashed before storage; raw bodies and
secrets never reach the database.

``execute`` runs the supplied callback inside the caller's database
transaction. The callback receives the open connection and must perform its
business writes through it without managing transactions itself, so the
business rows and the recorded idempotency result commit or roll back together.
"""

import hashlib
import json
import sqlite3

import database


MAX_REQUEST_KEY_LENGTH = 255
MAX_ENDPOINT_LENGTH = 255
MAX_REQUEST_BYTES = 1024 * 1024

_JSON_OPTIONS = {
    "sort_keys": True,
    "separators": (",", ":"),
    "ensure_ascii": False,
    "allow_nan": False,
}


class IdempotencyError(Exception):
    """Base class for idempotency failures."""


class IdempotencyRequestError(IdempotencyError, ValueError):
    """The scope or logical request is invalid."""


class IdempotencyConflict(IdempotencyError):
    """A scope and key were reused with a different canonical request."""


def canonicalize_request(request):
    """Return deterministic UTF-8 JSON bytes for a validated logical request."""
    try:
        encoded = json.dumps(request, **_JSON_OPTIONS).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise IdempotencyRequestError(
            "request is not canonically JSON serializable"
        ) from error

    if len(encoded) > MAX_REQUEST_BYTES:
        raise IdempotencyRequestError(
            f"request must be at most {MAX_REQUEST_BYTES} bytes"
        )
    return encoded


def request_digest(request):
    """Return the SHA-256 hex digest of a canonicalized logical request."""
    return hashlib.sha256(canonicalize_request(request)).hexdigest()


def lookup(connection, scope, endpoint, request_key, request):
    """Return the stored ``(status, response)`` for this request, or ``None``.

    A separate entry point from :func:`execute` because the caller may have work
    to do *before* it can be run safely inside a transaction — a publish builds
    its Git commit outside one — and a replay must skip that work entirely: the
    stored answer is the answer, whatever the world looks like now.

    Raises :class:`IdempotencyConflict` when the key was used for a different
    request, which is the same thing :func:`execute` would have raised.
    """

    _validate_scope(scope, endpoint, request_key)
    digest = request_digest(request)
    existing = _lookup(connection, scope, endpoint, request_key)
    if existing is None:
        return None
    return _existing_result(existing, digest)


def execute(connection, scope, endpoint, request_key, request, callback):
    """Run ``callback`` once per scope and return its recorded result.

    ``callback(connection)`` must return ``(status, response)`` where ``status``
    is an HTTP status code and ``response`` is JSON serializable. An exact retry
    returns the stored ``(status, response)`` without invoking the callback
    again. Reusing a key with a different request raises
    :class:`IdempotencyConflict`.
    """
    _validate_scope(scope, endpoint, request_key)
    digest = request_digest(request)

    existing = _lookup(connection, scope, endpoint, request_key)
    if existing is not None:
        return _existing_result(existing, digest)

    with database.immediate_transaction(connection):
        status, response = _validated_result(callback(connection))
        response_json = _serialize_response(response)
        try:
            connection.execute(
                """
                INSERT INTO idempotency_results (
                    scope, endpoint, request_key, request_sha256,
                    response_status, response_json, created_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
                """,
                (
                    scope,
                    endpoint,
                    request_key,
                    digest,
                    status,
                    response_json,
                ),
            )
        except sqlite3.IntegrityError:
            # A concurrent request with the same scope won through the primary
            # key. Discard this attempt's business rows and serve the winner.
            connection.rollback()
            existing = _lookup(connection, scope, endpoint, request_key)
            if existing is None:
                raise
            return _existing_result(existing, digest)

    return status, json.loads(response_json)


def _validate_scope(scope, endpoint, request_key):
    _validate_identifier(scope, "scope", None)
    _validate_identifier(endpoint, "endpoint", MAX_ENDPOINT_LENGTH)
    _validate_identifier(request_key, "request_key", MAX_REQUEST_KEY_LENGTH)


def _validate_identifier(value, name, maximum):
    if not isinstance(value, str) or value == "":
        raise IdempotencyRequestError(f"{name} must be a non-empty string")
    if maximum is not None and len(value) > maximum:
        raise IdempotencyRequestError(
            f"{name} must be at most {maximum} characters"
        )


def _validated_result(result):
    if not isinstance(result, tuple) or len(result) != 2:
        raise IdempotencyRequestError(
            "callback must return a (status, response) pair"
        )
    status, response = result
    if isinstance(status, bool) or not isinstance(status, int):
        raise IdempotencyRequestError("status must be an integer")
    if not 100 <= status <= 599:
        raise IdempotencyRequestError("status must be a valid HTTP status")
    return status, response


def _serialize_response(response):
    try:
        return json.dumps(response, **_JSON_OPTIONS)
    except (TypeError, ValueError) as error:
        raise IdempotencyRequestError(
            "response is not JSON serializable"
        ) from error


def _lookup(connection, scope, endpoint, request_key):
    return connection.execute(
        """
        SELECT request_sha256, response_status, response_json
        FROM idempotency_results
        WHERE scope = ? AND endpoint = ? AND request_key = ?
        """,
        (scope, endpoint, request_key),
    ).fetchone()


def _existing_result(row, digest):
    if row["request_sha256"] != digest:
        raise IdempotencyConflict(
            "request_key was reused with a different request"
        )
    return row["response_status"], json.loads(row["response_json"])
