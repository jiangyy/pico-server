"""Tests for Class, ownership, and membership operations."""

import tempfile
import unittest
import uuid
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

from accounts import (
    ForbiddenActionError,
    MissingUserError,
    User,
    create_user,
)
from classes import (
    ArchivedClassError,
    Class,
    InvalidClassNameError,
    Membership,
    MembershipNotFoundError,
    MissingClassError,
    RoleMismatchError,
    assign_student,
    change_class_owner,
    create_class,
    get_class,
    list_classes,
    list_memberships,
    remove_membership,
)
from database import open_database
from passwords import hash_password


FIXED_NOW = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
FIXED_NOW_TEXT = "2024-05-01T12:00:00.000000Z"


class ClassTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        database_path = Path(self.temporary_directory.name) / "app.db"
        self.connection = open_database(database_path)
        self.addCleanup(self.connection.close)

    # ----- controlled fixtures -------------------------------------------

    def insert_user(self, username, role, password="test password"):
        user = User(
            id=uuid.uuid4().hex,
            username=username,
            role=role,
            created_at="2024-01-01T00:00:00Z",
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO users (id, username, role, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user.id,
                    user.username,
                    user.role,
                    hash_password(password),
                    user.created_at,
                ),
            )
        return user

    def insert_admin(self):
        return self.insert_user("root", "admin")

    def insert_teacher(self, username="teacher"):
        return self.insert_user(username, "teacher")

    def insert_student(self, username="student"):
        return self.insert_user(username, "student")

    def insert_class(
        self,
        teacher_id,
        class_id=None,
        name="Class One",
        created_at="2024-01-01T00:00:00Z",
        archived_at=None,
    ):
        class_id = class_id or uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO classes (id, name, teacher_id, created_at, archived_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (class_id, name, teacher_id, created_at, archived_at),
            )
        return class_id

    def archive_class(self, class_id, archived_at="2024-02-01T00:00:00Z"):
        with self.connection:
            self.connection.execute(
                "UPDATE classes SET archived_at = ? WHERE id = ?",
                (archived_at, class_id),
            )

    def insert_membership(self, student_id, class_id, joined_at="2024-01-01T00:00:00Z"):
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO class_memberships (student_id, class_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (student_id, class_id, joined_at),
            )

    def membership_rows(self, student_id):
        return self.connection.execute(
            "SELECT * FROM class_memberships WHERE student_id = ?",
            (student_id,),
        ).fetchall()

    def membership_count(self):
        return self.connection.execute(
            "SELECT COUNT(*) FROM class_memberships"
        ).fetchone()[0]

    def class_count(self):
        return self.connection.execute("SELECT COUNT(*) FROM classes").fetchone()[0]

    def class_row(self, class_id):
        return self.connection.execute(
            "SELECT * FROM classes WHERE id = ?", (class_id,)
        ).fetchone()

    # ----- result objects --------------------------------------------------

    def test_result_objects_are_immutable(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        class_record = create_class(
            self.connection, admin, "Algebra", teacher.id, now=FIXED_NOW
        )

        with self.assertRaises(FrozenInstanceError):
            class_record.name = "Other"
        with self.assertRaises(FrozenInstanceError):
            class_record.teacher_id = teacher.id

        membership = Membership(
            student_id="student-1",
            class_id=class_record.id,
            joined_at=FIXED_NOW_TEXT,
            username="student-1",
        )
        with self.assertRaises(FrozenInstanceError):
            membership.class_id = "other"

    # ----- create_class ----------------------------------------------------

    def test_create_class_as_admin(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()

        class_record = create_class(
            self.connection, admin, "Algebra", teacher.id, now=FIXED_NOW
        )

        self.assertIsInstance(class_record, Class)
        self.assertEqual(class_record.name, "Algebra")
        self.assertEqual(class_record.teacher_id, teacher.id)
        self.assertEqual(class_record.created_at, FIXED_NOW_TEXT)
        self.assertIsNone(class_record.archived_at)

        row = self.class_row(class_record.id)
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "Algebra")
        self.assertEqual(row["teacher_id"], teacher.id)
        self.assertEqual(row["created_at"], FIXED_NOW_TEXT)
        self.assertIsNone(row["archived_at"])

    def test_create_class_strips_name(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()

        class_record = create_class(
            self.connection, admin, "  Algebra  ", teacher.id
        )

        self.assertEqual(class_record.name, "Algebra")
        self.assertEqual(self.class_row(class_record.id)["name"], "Algebra")

    def test_create_class_uses_public_user_helper(self):
        admin = User(
            id=uuid.uuid4().hex,
            username="root",
            role="admin",
            created_at="2024-01-01T00:00:00Z",
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO users (id, username, role, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    admin.id,
                    admin.username,
                    admin.role,
                    hash_password("secret"),
                    admin.created_at,
                ),
            )
        teacher = create_user(
            self.connection, admin, "teacher", "secret", "teacher"
        )

        class_record = create_class(self.connection, admin, "Algebra", teacher.id)

        self.assertEqual(class_record.teacher_id, teacher.id)

    def test_create_class_requires_admin(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        impostor = User(
            id=admin.id,
            username=admin.username,
            role="teacher",
            created_at=admin.created_at,
        )

        for actor in (teacher, student, impostor, None, "admin", admin.id):
            with self.subTest(actor=actor):
                with self.assertRaises(ForbiddenActionError):
                    create_class(self.connection, actor, "Algebra", teacher.id)

        self.assertEqual(self.class_count(), 0)

    def test_create_class_rejects_invalid_name(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()

        for name in ("", "   ", None, 3, ["Algebra"]):
            with self.subTest(name=name):
                with self.assertRaises(InvalidClassNameError):
                    create_class(self.connection, admin, name, teacher.id)

        self.assertEqual(self.class_count(), 0)

    def test_create_class_requires_existing_teacher(self):
        admin = self.insert_admin()
        student = self.insert_student()

        with self.assertRaises(MissingUserError):
            create_class(self.connection, admin, "Algebra", uuid.uuid4().hex)
        with self.assertRaises(RoleMismatchError):
            create_class(self.connection, admin, "Algebra", student.id)
        with self.assertRaises(RoleMismatchError):
            create_class(self.connection, admin, "Algebra", admin.id)

        self.assertEqual(self.class_count(), 0)

    # ----- change_class_owner ---------------------------------------------

    def test_change_class_owner_replaces_teacher(self):
        admin = self.insert_admin()
        first = self.insert_teacher("first")
        second = self.insert_teacher("second")
        class_id = self.insert_class(first.id)

        result = change_class_owner(self.connection, admin, class_id, second.id)

        self.assertEqual(result.id, class_id)
        self.assertEqual(result.teacher_id, second.id)
        self.assertEqual(result.name, "Class One")
        self.assertEqual(self.class_row(class_id)["teacher_id"], second.id)

    def test_change_class_owner_requires_admin(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        class_id = self.insert_class(teacher.id)
        impostor = User(
            id=admin.id,
            username=admin.username,
            role="student",
            created_at=admin.created_at,
        )

        for actor in (teacher, student, impostor, None, "admin"):
            with self.subTest(actor=actor):
                with self.assertRaises(ForbiddenActionError):
                    change_class_owner(
                        self.connection, actor, class_id, teacher.id
                    )

        self.assertEqual(self.class_row(class_id)["teacher_id"], teacher.id)

    def test_change_class_owner_rejects_missing_class(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()

        with self.assertRaises(MissingClassError):
            change_class_owner(
                self.connection, admin, uuid.uuid4().hex, teacher.id
            )

    def test_change_class_owner_rejects_missing_or_wrong_teacher(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        class_id = self.insert_class(teacher.id)

        with self.assertRaises(MissingUserError):
            change_class_owner(
                self.connection, admin, class_id, uuid.uuid4().hex
            )
        with self.assertRaises(RoleMismatchError):
            change_class_owner(self.connection, admin, class_id, student.id)
        with self.assertRaises(RoleMismatchError):
            change_class_owner(self.connection, admin, class_id, admin.id)

        self.assertEqual(self.class_row(class_id)["teacher_id"], teacher.id)

    # ----- assign_student ---------------------------------------------------

    def test_assign_student_creates_single_membership(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        class_id = self.insert_class(teacher.id)

        membership = assign_student(
            self.connection, admin, student.id, class_id, now=FIXED_NOW
        )

        self.assertIsInstance(membership, Membership)
        self.assertEqual(membership.student_id, student.id)
        self.assertEqual(membership.class_id, class_id)
        self.assertEqual(membership.joined_at, FIXED_NOW_TEXT)

        rows = self.membership_rows(student.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["class_id"], class_id)
        self.assertEqual(rows[0]["joined_at"], FIXED_NOW_TEXT)

    def test_assign_student_membership_carries_username(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student("alice")
        class_id = self.insert_class(teacher.id)

        membership = assign_student(
            self.connection, admin, student.id, class_id, now=FIXED_NOW
        )
        self.assertEqual(membership.username, "alice")

        again = assign_student(
            self.connection, admin, student.id, class_id, now=FIXED_NOW
        )
        self.assertEqual(again.username, "alice")

    def test_list_memberships_shows_username_to_owner_and_admin(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student("alice")
        class_id = self.insert_class(teacher.id)
        assign_student(
            self.connection, admin, student.id, class_id, now=FIXED_NOW
        )

        for actor in (admin, teacher):
            with self.subTest(role=actor.role):
                members = list_memberships(self.connection, actor, class_id)
                self.assertEqual(len(members), 1)
                self.assertEqual(members[0].student_id, student.id)
                self.assertEqual(members[0].class_id, class_id)
                self.assertEqual(members[0].username, "alice")

    def test_list_memberships_denies_non_owner_and_student(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        other_teacher = self.insert_teacher("other")
        student = self.insert_student("alice")
        class_id = self.insert_class(teacher.id)
        assign_student(
            self.connection, admin, student.id, class_id, now=FIXED_NOW
        )

        with self.assertRaises(ForbiddenActionError):
            list_memberships(self.connection, other_teacher, class_id)
        with self.assertRaises(ForbiddenActionError):
            list_memberships(self.connection, student, class_id)
        with self.assertRaises(MissingClassError):
            list_memberships(self.connection, admin, "missing-class")

    def test_assign_student_same_class_is_idempotent(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        class_id = self.insert_class(teacher.id)

        first = assign_student(
            self.connection, admin, student.id, class_id, now=FIXED_NOW
        )
        second = assign_student(
            self.connection,
            admin,
            student.id,
            class_id,
            now=datetime(2024, 6, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(first, second)
        self.assertEqual(len(self.membership_rows(student.id)), 1)
        self.assertEqual(
            self.membership_rows(student.id)[0]["joined_at"], FIXED_NOW_TEXT
        )

    def test_assign_student_transfers_between_classes(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        first_class = self.insert_class(teacher.id, class_id="class-1")
        second_class = self.insert_class(teacher.id, class_id="class-2")
        self.insert_membership(student.id, first_class)

        membership = assign_student(
            self.connection,
            admin,
            student.id,
            second_class,
            now=FIXED_NOW,
        )

        self.assertEqual(membership.class_id, second_class)
        rows = self.membership_rows(student.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["student_id"], student.id)
        self.assertEqual(rows[0]["class_id"], second_class)
        self.assertEqual(rows[0]["joined_at"], FIXED_NOW_TEXT)

    def test_assign_student_requires_admin(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        class_id = self.insert_class(teacher.id)
        impostor = User(
            id=admin.id,
            username=admin.username,
            role="teacher",
            created_at=admin.created_at,
        )

        for actor in (teacher, student, impostor, None, "admin"):
            with self.subTest(actor=actor):
                with self.assertRaises(ForbiddenActionError):
                    assign_student(
                        self.connection, actor, student.id, class_id
                    )

        self.assertEqual(self.membership_count(), 0)

    def test_assign_student_rejects_missing_or_wrong_student(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        class_id = self.insert_class(teacher.id)

        with self.assertRaises(MissingUserError):
            assign_student(
                self.connection, admin, uuid.uuid4().hex, class_id
            )
        with self.assertRaises(RoleMismatchError):
            assign_student(self.connection, admin, teacher.id, class_id)
        with self.assertRaises(RoleMismatchError):
            assign_student(self.connection, admin, admin.id, class_id)

        self.assertEqual(self.membership_count(), 0)

    def test_assign_student_rejects_missing_class(self):
        admin = self.insert_admin()
        student = self.insert_student()

        with self.assertRaises(MissingClassError):
            assign_student(
                self.connection, admin, student.id, uuid.uuid4().hex
            )

        self.assertEqual(self.membership_count(), 0)

    def test_assign_student_rejects_archived_class(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        class_id = self.insert_class(teacher.id)
        self.archive_class(class_id)

        with self.assertRaises(ArchivedClassError):
            assign_student(self.connection, admin, student.id, class_id)

        self.assertEqual(self.membership_count(), 0)
        self.assertIsNotNone(self.class_row(class_id))

    def test_assign_student_archived_transfer_keeps_current_membership(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        current_class = self.insert_class(teacher.id, class_id="class-1")
        archived_class = self.insert_class(teacher.id, class_id="class-2")
        self.insert_membership(student.id, current_class)
        self.archive_class(archived_class)

        with self.assertRaises(ArchivedClassError):
            assign_student(self.connection, admin, student.id, archived_class)

        rows = self.membership_rows(student.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["class_id"], current_class)

    def test_assign_student_rejects_transfer_from_archived_class(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        archived_class = self.insert_class(teacher.id, class_id="class-1")
        live_class = self.insert_class(teacher.id, class_id="class-2")
        self.insert_membership(
            student.id,
            archived_class,
            joined_at="2024-03-01T00:00:00Z",
        )
        self.archive_class(archived_class)

        with self.assertRaises(ArchivedClassError):
            assign_student(
                self.connection, admin, student.id, live_class, now=FIXED_NOW
            )

        rows = self.membership_rows(student.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["student_id"], student.id)
        self.assertEqual(rows[0]["class_id"], archived_class)
        self.assertEqual(rows[0]["joined_at"], "2024-03-01T00:00:00Z")
        self.assertEqual(self.membership_count(), 1)

    # ----- remove_membership -----------------------------------------------

    def test_remove_membership_deletes_row(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        class_id = self.insert_class(teacher.id)
        self.insert_membership(student.id, class_id)

        removed = remove_membership(
            self.connection, admin, class_id, student.id
        )

        self.assertTrue(removed)
        self.assertEqual(self.membership_count(), 0)
        self.assertIsNotNone(self.class_row(class_id))

    def test_remove_membership_retry_is_safe_noop(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        class_id = self.insert_class(teacher.id)

        self.assertFalse(
            remove_membership(self.connection, admin, class_id, student.id)
        )

        self.insert_membership(student.id, class_id)
        self.assertTrue(
            remove_membership(self.connection, admin, class_id, student.id)
        )
        self.assertFalse(
            remove_membership(self.connection, admin, class_id, student.id)
        )
        self.assertEqual(self.membership_count(), 0)

    def test_remove_membership_requires_admin(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        class_id = self.insert_class(teacher.id)
        self.insert_membership(student.id, class_id)
        impostor = User(
            id=admin.id,
            username=admin.username,
            role="student",
            created_at=admin.created_at,
        )

        for actor in (teacher, student, impostor, None, "admin"):
            with self.subTest(actor=actor):
                with self.assertRaises(ForbiddenActionError):
                    remove_membership(
                        self.connection, actor, class_id, student.id
                    )

        self.assertEqual(self.membership_count(), 1)

    def test_remove_membership_requires_existing_student(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        class_id = self.insert_class(teacher.id)

        with self.assertRaises(MissingUserError):
            remove_membership(
                self.connection, admin, class_id, uuid.uuid4().hex
            )

    def test_remove_membership_requires_existing_class(self):
        admin = self.insert_admin()
        student = self.insert_student()

        with self.assertRaises(MissingClassError):
            remove_membership(
                self.connection, admin, uuid.uuid4().hex, student.id
            )

    def test_remove_membership_rejects_non_student(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        class_id = self.insert_class(teacher.id)

        with self.assertRaises(RoleMismatchError):
            remove_membership(self.connection, admin, class_id, teacher.id)
        with self.assertRaises(RoleMismatchError):
            remove_membership(self.connection, admin, class_id, admin.id)

    def test_remove_membership_rejects_archived_class(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        class_id = self.insert_class(teacher.id)
        self.insert_membership(student.id, class_id)
        self.archive_class(class_id)

        with self.assertRaises(ArchivedClassError):
            remove_membership(self.connection, admin, class_id, student.id)

        self.assertEqual(self.membership_count(), 1)

    def test_remove_membership_wrong_class_is_not_found_and_preserved(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        current_class = self.insert_class(teacher.id, class_id="class-1")
        other_class = self.insert_class(teacher.id, class_id="class-2")
        self.insert_membership(
            student.id,
            current_class,
            joined_at="2024-03-01T00:00:00Z",
        )

        with self.assertRaises(MembershipNotFoundError):
            remove_membership(
                self.connection, admin, other_class, student.id
            )

        rows = self.membership_rows(student.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["student_id"], student.id)
        self.assertEqual(rows[0]["class_id"], current_class)
        self.assertEqual(rows[0]["joined_at"], "2024-03-01T00:00:00Z")
        self.assertEqual(self.membership_count(), 1)

    # ----- get_class --------------------------------------------------------

    def test_get_class_admin_reads_any_class(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        class_id = self.insert_class(teacher.id)

        result = get_class(self.connection, admin, class_id)

        self.assertEqual(result.id, class_id)

    def test_get_class_teacher_reads_owned_only(self):
        admin = self.insert_admin()
        owner = self.insert_teacher("owner")
        other = self.insert_teacher("other")
        owned = self.insert_class(owner.id)
        foreign = self.insert_class(other.id)

        self.assertEqual(get_class(self.connection, owner, owned).id, owned)
        with self.assertRaises(ForbiddenActionError):
            get_class(self.connection, owner, foreign)

    def test_get_class_student_reads_current_only(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        current = self.insert_class(teacher.id, class_id="class-1")
        foreign = self.insert_class(teacher.id, class_id="class-2")
        self.insert_membership(student.id, current)

        self.assertEqual(get_class(self.connection, student, current).id, current)
        with self.assertRaises(ForbiddenActionError):
            get_class(self.connection, student, foreign)

    def test_get_class_missing_class(self):
        admin = self.insert_admin()

        with self.assertRaises(MissingClassError):
            get_class(self.connection, admin, uuid.uuid4().hex)

    def test_get_class_rejects_invalid_actor(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        class_id = self.insert_class(teacher.id)

        for actor in (None, "admin", 3, admin.id):
            with self.subTest(actor=actor):
                with self.assertRaises(ForbiddenActionError):
                    get_class(self.connection, actor, class_id)

    def test_get_class_allows_archived_read(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        class_id = self.insert_class(teacher.id)
        self.archive_class(class_id)

        result = get_class(self.connection, admin, class_id)

        self.assertEqual(result.id, class_id)
        self.assertEqual(result.archived_at, "2024-02-01T00:00:00Z")

    # ----- list_classes ----------------------------------------------------

    def test_list_classes_admin_returns_all_ordered_by_id(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        self.insert_class(teacher.id, class_id="class-3")
        self.insert_class(teacher.id, class_id="class-1")
        self.insert_class(teacher.id, class_id="class-2")

        result = list_classes(self.connection, admin)

        self.assertEqual([item.id for item in result], ["class-1", "class-2", "class-3"])

    def test_list_classes_teacher_returns_owned_ordered(self):
        admin = self.insert_admin()
        owner = self.insert_teacher("owner")
        other = self.insert_teacher("other")
        self.insert_class(owner.id, class_id="class-2")
        self.insert_class(owner.id, class_id="class-1")
        self.insert_class(other.id, class_id="class-0")

        result = list_classes(self.connection, owner)

        self.assertEqual([item.id for item in result], ["class-1", "class-2"])
        self.assertTrue(all(item.teacher_id == owner.id for item in result))

    def test_list_classes_student_returns_current_only(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        self.insert_class(teacher.id, class_id="class-1")
        current = self.insert_class(teacher.id, class_id="class-2")
        self.insert_membership(student.id, current)

        result = list_classes(self.connection, student)

        self.assertEqual([item.id for item in result], [current])

    def test_list_classes_student_without_membership_is_empty(self):
        student = self.insert_student()

        self.assertEqual(list_classes(self.connection, student), [])

    def test_list_classes_rejects_invalid_actor(self):
        for actor in (None, "admin", 3):
            with self.subTest(actor=actor):
                with self.assertRaises(ForbiddenActionError):
                    list_classes(self.connection, actor)

    def test_list_classes_rejects_unknown_role(self):
        unknown = User(
            id=uuid.uuid4().hex,
            username="ghost",
            role="owner",
            created_at="2024-01-01T00:00:00Z",
        )

        with self.assertRaises(ForbiddenActionError):
            list_classes(self.connection, unknown)

    # ----- schema invariant -------------------------------------------------

    def test_membership_changes_only_touch_one_link_table(self):
        admin = self.insert_admin()
        teacher = self.insert_teacher()
        student = self.insert_student()
        first = self.insert_class(teacher.id, class_id="class-1")
        second = self.insert_class(teacher.id, class_id="class-2")

        assign_student(self.connection, admin, student.id, first)
        assign_student(self.connection, admin, student.id, second)
        assign_student(self.connection, admin, student.id, second)

        rows = self.membership_rows(student.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["class_id"], second)

        membership_tables = {
            row["name"]
            for row in self.connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name LIKE '%membership%'
                """
            )
        }
        self.assertEqual(membership_tables, {"class_memberships"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
