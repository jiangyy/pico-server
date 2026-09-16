"""Account operations for PicoPG."""

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from passwords import hash_password


MAX_USERNAME_LENGTH = 64
ROLES = ("student", "teacher", "admin")


class UsernameTakenError(Exception):
    """Raised when a username already belongs to an account."""


class ForbiddenActionError(Exception):
    """Raised when the actor is not allowed to perform the action."""


class MissingUserError(Exception):
    """Raised when the target user does not exist."""


class InvalidRoleError(Exception):
    """Raised when a role is not one of the supported roles."""


class RoleConflictError(Exception):
    """Raised when a role change conflicts with existing relationships."""


class StoredUserError(Exception):
    """A durable user row cannot be represented by the admin API."""


@dataclass(frozen=True)
class User:
    id: str
    username: str
    role: str
    created_at: str


@dataclass(frozen=True)
class AdminUser:
    """One bounded user record for admin discovery (no credentials)."""

    id: str
    role: str
    created_at: str


@dataclass(frozen=True)
class UserPage:
    """One keyset page of users plus the exclusive next key."""

    items: tuple
    next_key: str | None


def _require_admin(actor):
    if not isinstance(actor, User) or actor.role != "admin":
        raise ForbiddenActionError("admin role required")


def _validate_role(role):
    if role not in ROLES:
        raise InvalidRoleError(role)


def _validate_credentials(username, password):
    if not isinstance(username, str):
        raise TypeError("username must be a string")
    username = username.strip()
    if not username or len(username) > MAX_USERNAME_LENGTH:
        raise ValueError("username must contain 1 to 64 characters")
    if not isinstance(password, str):
        raise TypeError("password must be a string")
    if not password:
        raise ValueError("password must not be empty")
    return username


def _new_user(username, role):
    return User(
        id=uuid.uuid4().hex,
        username=username,
        role=role,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def _insert_user(connection, user, password):
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
            raise UsernameTakenError(user.username)

    return user


def register_student(connection, username, password):
    """Create and return a Student account."""
    username = _validate_credentials(username, password)
    user = _new_user(username, "student")
    return _insert_user(connection, user, password)


def create_user(connection, actor, username, password, role):
    """Create and return a managed account. Admin only."""
    _require_admin(actor)
    _validate_role(role)
    username = _validate_credentials(username, password)
    user = _new_user(username, role)
    return _insert_user(connection, user, password)


def list_users(connection, actor, *, limit, after=None):
    """Return one Admin-only keyset page of users ordered by id.

    The query uses ``LIMIT limit + 1`` at the authoritative boundary, so a
    page never loads the whole table.  Only the public identifier, role, and
    creation time are returned; password material and session state are never
    read.
    """

    _require_admin(actor)
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")

    parameters = []
    where = ""
    if after is not None:
        where = "WHERE id > ?"
        parameters.append(after)
    parameters.append(limit + 1)
    rows = connection.execute(
        "SELECT id, role, created_at FROM users "
        f"{where} ORDER BY id LIMIT ?",
        parameters,
    ).fetchall()

    page_rows = rows[:limit]
    next_key = page_rows[-1]["id"] if len(rows) > limit else None
    items = []
    for row in page_rows:
        if type(row["id"]) is not str or not row["id"]:
            raise StoredUserError("stored user identifier is invalid")
        items.append(
            AdminUser(
                id=row["id"],
                role=row["role"],
                created_at=row["created_at"],
            )
        )
    return UserPage(items=tuple(items), next_key=next_key)


def change_user_role(connection, actor, user_id, new_role):
    """Change an existing user's role. Admin only."""
    _require_admin(actor)
    _validate_role(new_role)

    with connection:
        row = connection.execute(
            "SELECT id, username, role, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            raise MissingUserError(user_id)

        current_role = row["role"]
        target = User(
            id=row["id"],
            username=row["username"],
            role=current_role,
            created_at=row["created_at"],
        )
        if current_role == new_role:
            return target

        if current_role == "teacher":
            owns_class = connection.execute(
                "SELECT 1 FROM classes WHERE teacher_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if owns_class is not None:
                raise RoleConflictError("teacher owns at least one class")

        if current_role == "student":
            has_membership = connection.execute(
                "SELECT 1 FROM class_memberships WHERE student_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if has_membership is not None:
                raise RoleConflictError("student has a current class membership")

        connection.execute(
            "UPDATE users SET role = ? WHERE id = ?", (new_role, user_id)
        )

    return replace(target, role=new_role)
