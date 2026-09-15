"""Class, ownership, and membership operations for PicoPG."""

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from accounts import ForbiddenActionError, MissingUserError, User


class MissingClassError(Exception):
    """Raised when a referenced Class does not exist."""


class InvalidClassNameError(Exception):
    """Raised when a Class name is not a valid, non-empty string."""


class RoleMismatchError(Exception):
    """Raised when an account has the wrong role for the operation."""


class ArchivedClassError(Exception):
    """Raised when a membership change targets an archived Class."""


class MembershipNotFoundError(Exception):
    """Raised when a Student's current membership is in a different Class."""


@dataclass(frozen=True)
class Class:
    id: str
    name: str
    teacher_id: str
    created_at: str
    archived_at: str


@dataclass(frozen=True)
class Membership:
    student_id: str
    class_id: str
    joined_at: str


def _require_admin(actor):
    if not isinstance(actor, User) or actor.role != "admin":
        raise ForbiddenActionError("admin role required")


def _require_user_actor(actor):
    if not isinstance(actor, User):
        raise ForbiddenActionError("valid account required")


def _require_user(connection, user_id):
    row = connection.execute(
        "SELECT id, username, role, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        raise MissingUserError(user_id)
    return row


def _require_teacher(connection, user_id):
    row = _require_user(connection, user_id)
    if row["role"] != "teacher":
        raise RoleMismatchError("class owner must be a teacher")
    return row


def _require_student(connection, user_id):
    row = _require_user(connection, user_id)
    if row["role"] != "student":
        raise RoleMismatchError("member must be a student")
    return row


def _validate_name(name):
    if not isinstance(name, str):
        raise InvalidClassNameError("class name must be a string")
    name = name.strip()
    if not name:
        raise InvalidClassNameError("class name must not be empty")
    return name


def _utc_now(value):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("class time must include a timezone")
    return value.astimezone(timezone.utc)


def _format_time(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _class_from_row(row):
    return Class(
        id=row["id"],
        name=row["name"],
        teacher_id=row["teacher_id"],
        created_at=row["created_at"],
        archived_at=row["archived_at"],
    )


def _membership_from_row(row):
    return Membership(
        student_id=row["student_id"],
        class_id=row["class_id"],
        joined_at=row["joined_at"],
    )


def _require_class(connection, class_id):
    row = connection.execute(
        "SELECT * FROM classes WHERE id = ?", (class_id,)
    ).fetchone()
    if row is None:
        raise MissingClassError(class_id)
    return row


def create_class(connection, actor, name, teacher_id, now=None):
    """Create a Class owned by an existing Teacher. Admin only."""
    _require_admin(actor)
    name = _validate_name(name)
    _require_teacher(connection, teacher_id)

    class_record = Class(
        id=uuid.uuid4().hex,
        name=name,
        teacher_id=teacher_id,
        created_at=_format_time(_utc_now(now)),
        archived_at=None,
    )
    with connection:
        connection.execute(
            """
            INSERT INTO classes (id, name, teacher_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                class_record.id,
                class_record.name,
                class_record.teacher_id,
                class_record.created_at,
            ),
        )
    return class_record


def change_class_owner(connection, actor, class_id, teacher_id):
    """Replace a Class owner with another existing Teacher. Admin only."""
    _require_admin(actor)
    row = _require_class(connection, class_id)
    _require_teacher(connection, teacher_id)

    with connection:
        connection.execute(
            "UPDATE classes SET teacher_id = ? WHERE id = ?",
            (teacher_id, class_id),
        )
    return replace(_class_from_row(row), teacher_id=teacher_id)


def assign_student(connection, actor, student_id, class_id, now=None):
    """Assign a Student to a Class, transferring any current membership."""
    _require_admin(actor)
    _require_student(connection, student_id)
    joined_at = _format_time(_utc_now(now))

    with connection:
        class_row = connection.execute(
            "SELECT * FROM classes WHERE id = ?", (class_id,)
        ).fetchone()
        if class_row is None:
            raise MissingClassError(class_id)
        if class_row["archived_at"] is not None:
            raise ArchivedClassError(class_id)

        existing = connection.execute(
            """
            SELECT class_memberships.student_id,
                   class_memberships.class_id,
                   class_memberships.joined_at,
                   classes.archived_at AS current_archived_at
            FROM class_memberships
            JOIN classes ON classes.id = class_memberships.class_id
            WHERE class_memberships.student_id = ?
            """,
            (student_id,),
        ).fetchone()
        if existing is not None and existing["current_archived_at"] is not None:
            raise ArchivedClassError(existing["class_id"])
        if existing is not None and existing["class_id"] == class_id:
            return _membership_from_row(existing)

        if existing is None:
            connection.execute(
                """
                INSERT INTO class_memberships (student_id, class_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (student_id, class_id, joined_at),
            )
        else:
            connection.execute(
                """
                UPDATE class_memberships
                SET class_id = ?, joined_at = ?
                WHERE student_id = ?
                """,
                (class_id, joined_at, student_id),
            )

    return Membership(
        student_id=student_id,
        class_id=class_id,
        joined_at=joined_at,
    )


def remove_membership(connection, actor, class_id, student_id):
    """Remove a Student's membership in a specific Class. Admin only.

    Returns ``True`` when the matching membership row was removed and ``False``
    when the Student has no current membership. A Student who is a member of a
    different Class raises :class:`MembershipNotFoundError` and that membership
    is left unchanged.
    """
    _require_admin(actor)
    _require_student(connection, student_id)
    class_row = _require_class(connection, class_id)

    with connection:
        row = connection.execute(
            """
            SELECT class_id
            FROM class_memberships
            WHERE student_id = ?
            """,
            (student_id,),
        ).fetchone()
        if row is None:
            return False
        if row["class_id"] != class_id:
            raise MembershipNotFoundError(student_id)
        if class_row["archived_at"] is not None:
            raise ArchivedClassError(class_id)

        connection.execute(
            """
            DELETE FROM class_memberships
            WHERE student_id = ? AND class_id = ?
            """,
            (student_id, class_id),
        )
    return True


def _authorize_class_read(connection, actor, row):
    if actor.role == "admin":
        return
    if actor.role == "teacher":
        if row["teacher_id"] == actor.id:
            return
        raise ForbiddenActionError("teacher does not own class")
    if actor.role == "student":
        membership = connection.execute(
            """
            SELECT 1 FROM class_memberships
            WHERE student_id = ? AND class_id = ?
            """,
            (actor.id, row["id"]),
        ).fetchone()
        if membership is not None:
            return
        raise ForbiddenActionError("student is not a member")
    raise ForbiddenActionError("unknown role")


def get_class(connection, actor, class_id):
    """Return a single Class when the actor is allowed to read it."""
    _require_user_actor(actor)
    row = _require_class(connection, class_id)
    _authorize_class_read(connection, actor, row)
    return _class_from_row(row)


def list_classes(connection, actor):
    """Return readable Classes ordered by ID."""
    _require_user_actor(actor)

    if actor.role == "admin":
        rows = connection.execute(
            "SELECT * FROM classes ORDER BY id"
        ).fetchall()
    elif actor.role == "teacher":
        rows = connection.execute(
            "SELECT * FROM classes WHERE teacher_id = ? ORDER BY id",
            (actor.id,),
        ).fetchall()
    elif actor.role == "student":
        rows = connection.execute(
            """
            SELECT classes.* FROM classes
            JOIN class_memberships
                ON class_memberships.class_id = classes.id
            WHERE class_memberships.student_id = ?
            ORDER BY classes.id
            """,
            (actor.id,),
        ).fetchall()
    else:
        raise ForbiddenActionError("unknown role")

    return [_class_from_row(row) for row in rows]
