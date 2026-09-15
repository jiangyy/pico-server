"""Account operations for PicoPG."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from passwords import hash_password


MAX_USERNAME_LENGTH = 64


class UsernameTakenError(Exception):
    """Raised when a username already belongs to an account."""


@dataclass(frozen=True)
class User:
    id: str
    username: str
    role: str
    created_at: str


def register_student(connection, username, password):
    """Create and return a Student account."""
    if not isinstance(username, str):
        raise TypeError("username must be a string")
    username = username.strip()
    if not username or len(username) > MAX_USERNAME_LENGTH:
        raise ValueError("username must contain 1 to 64 characters")
    if not isinstance(password, str):
        raise TypeError("password must be a string")
    if not password:
        raise ValueError("password must not be empty")

    user = User(
        id=uuid.uuid4().hex,
        username=username,
        role="student",
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    password_hash = hash_password(password)

    with connection:
        cursor = connection.execute(
            """
            INSERT INTO users (id, username, role, password_hash, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(username) DO NOTHING
            """,
            (user.id, user.username, user.role, password_hash, user.created_at),
        )
        if cursor.rowcount == 0:
            raise UsernameTakenError(username)

    return user
