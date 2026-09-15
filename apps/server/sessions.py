"""Session lifecycle for PicoPG accounts."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from accounts import User
from passwords import hash_password, verify_password


SESSION_LIFETIME = timedelta(days=30)
TOKEN_BYTES = 32
_DUMMY_PASSWORD_HASH = hash_password("invalid account password")


class InvalidCredentialsError(Exception):
    """Raised when login credentials do not match an account."""


class InvalidSessionError(Exception):
    """Raised when a session is missing, expired, or revoked."""


@dataclass(frozen=True)
class Session:
    token: str
    expires_at: str
    user: User


def login(connection, username, password, now=None):
    """Verify credentials, persist a session, and return its one-time token."""
    if not isinstance(username, str) or not isinstance(password, str):
        raise InvalidCredentialsError

    row = connection.execute(
        "SELECT * FROM users WHERE username = ?", (username.strip(),)
    ).fetchone()
    encoded_hash = row["password_hash"] if row is not None else _DUMMY_PASSWORD_HASH
    password_matches = verify_password(password, encoded_hash)
    if row is None or not password_matches:
        raise InvalidCredentialsError

    created = _utc_now(now)
    expires = created + SESSION_LIFETIME
    token = new_session_token()
    token_hash = hash_session_token(token)

    with connection:
        connection.execute(
            """
            INSERT INTO sessions (token_hash, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                token_hash,
                row["id"],
                _format_time(created),
                _format_time(expires),
            ),
        )

    return Session(
        token=token,
        expires_at=_format_time(expires),
        user=_user_from_row(row),
    )


def authenticate_session(connection, token, now=None):
    """Return the user for an active bearer token."""
    try:
        token_hash = hash_session_token(token)
    except (TypeError, ValueError):
        raise InvalidSessionError from None

    row = connection.execute(
        """
        SELECT users.id, users.username, users.role, users.created_at
        FROM sessions
        JOIN users ON users.id = sessions.user_id
        WHERE sessions.token_hash = ?
          AND sessions.revoked_at IS NULL
          AND sessions.expires_at > ?
        """,
        (token_hash, _format_time(_utc_now(now))),
    ).fetchone()
    if row is None:
        raise InvalidSessionError
    return _user_from_row(row)


def revoke_session(connection, token, now=None):
    """Revoke a bearer token, returning whether an active row changed."""
    try:
        token_hash = hash_session_token(token)
    except (TypeError, ValueError):
        return False

    with connection:
        cursor = connection.execute(
            """
            UPDATE sessions
            SET revoked_at = ?
            WHERE token_hash = ? AND revoked_at IS NULL
            """,
            (_format_time(_utc_now(now)), token_hash),
        )
    return cursor.rowcount == 1


def new_session_token():
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_session_token(token):
    if not isinstance(token, str):
        raise TypeError("session token must be a string")
    if not token:
        raise ValueError("session token must not be empty")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc_now(value):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("session time must include a timezone")
    return value.astimezone(timezone.utc)


def _format_time(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _user_from_row(row):
    return User(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        created_at=row["created_at"],
    )
