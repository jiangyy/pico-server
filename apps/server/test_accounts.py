"""Tests for account operations."""

import tempfile
import unittest
import uuid
from pathlib import Path

from accounts import (
    MAX_USERNAME_LENGTH,
    ForbiddenActionError,
    InvalidRoleError,
    MissingUserError,
    RoleConflictError,
    UsernameTakenError,
    User,
    change_user_role,
    create_user,
    list_users,
    register_student,
)
from database import open_database
from passwords import hash_password, verify_password


class AccountTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        database_path = Path(self.temporary_directory.name) / "app.db"
        self.connection = open_database(database_path)
        self.addCleanup(self.connection.close)

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
                (user.id, user.username, user.role, hash_password(password), user.created_at),
            )
        return user

    def insert_admin(self):
        return self.insert_user("root", "admin")

    def user_count(self):
        return self.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def role_of(self, user_id):
        return self.connection.execute(
            "SELECT role FROM users WHERE id = ?", (user_id,)
        ).fetchone()["role"]

    def insert_class(self, teacher_id, name="Class One"):
        class_id = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO classes (id, name, teacher_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (class_id, name, teacher_id, "2024-01-01T00:00:00Z"),
            )
        return class_id

    def insert_membership(self, student_id, class_id):
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO class_memberships (student_id, class_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (student_id, class_id, "2024-01-01T00:00:00Z"),
            )

    def test_register_student(self):
        user = register_student(self.connection, "  alice  ", "secret")

        row = self.connection.execute(
            "SELECT * FROM users WHERE id = ?", (user.id,)
        ).fetchone()
        self.assertEqual(user.username, "alice")
        self.assertEqual(user.role, "student")
        self.assertEqual(row["username"], user.username)
        self.assertEqual(row["role"], "student")
        self.assertTrue(verify_password("secret", row["password_hash"]))
        self.assertNotIn("password", vars(user))

    def test_registration_rejects_invalid_input(self):
        cases = (
            ("", "secret"),
            (" " * 3, "secret"),
            ("a" * (MAX_USERNAME_LENGTH + 1), "secret"),
            ("alice", ""),
        )

        for username, password in cases:
            with self.subTest(username=username, password=password):
                with self.assertRaises(ValueError):
                    register_student(self.connection, username, password)

        count = self.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        self.assertEqual(count, 0)

    def test_registration_rejects_duplicate_username(self):
        original = register_student(self.connection, "alice", "first password")

        with self.assertRaises(UsernameTakenError):
            register_student(self.connection, "alice", "second password")

        rows = self.connection.execute("SELECT * FROM users").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], original.id)
        self.assertTrue(verify_password("first password", rows[0]["password_hash"]))

    def test_create_user_creates_each_role(self):
        admin = self.insert_admin()

        for role in ("student", "teacher", "admin"):
            with self.subTest(role=role):
                user = create_user(
                    self.connection, admin, f"user-{role}", "secret", role
                )

                row = self.connection.execute(
                    "SELECT * FROM users WHERE id = ?", (user.id,)
                ).fetchone()
                self.assertEqual(user.username, f"user-{role}")
                self.assertEqual(user.role, role)
                self.assertEqual(row["username"], user.username)
                self.assertEqual(row["role"], role)
                self.assertTrue(verify_password("secret", row["password_hash"]))
                self.assertNotIn("password", vars(user))
                self.assertNotIn("password", user.role)

    def test_create_user_normalizes_username(self):
        admin = self.insert_admin()

        user = create_user(self.connection, admin, "  bob  ", "secret", "student")

        self.assertEqual(user.username, "bob")

    def test_create_user_requires_admin_actor(self):
        admin = self.insert_admin()
        student = self.insert_user("alice", "student")
        teacher = self.insert_user("carol", "teacher")

        impostor = User(
            id=admin.id,
            username=admin.username,
            role="student",
            created_at=admin.created_at,
        )
        actors = (student, teacher, impostor, None, "admin", admin.id)

        for actor in actors:
            with self.subTest(actor=actor):
                with self.assertRaises(ForbiddenActionError):
                    create_user(self.connection, actor, "mallory", "secret", "admin")

        self.assertIsNone(
            self.connection.execute(
                "SELECT * FROM users WHERE username = 'mallory'"
            ).fetchone()
        )

    def test_create_user_rejects_invalid_role(self):
        admin = self.insert_admin()
        before = self.user_count()

        for role in ("owner", "Administrator", "", None, 3):
            with self.subTest(role=role):
                with self.assertRaises(InvalidRoleError):
                    create_user(self.connection, admin, "mallory", "secret", role)

        self.assertEqual(self.user_count(), before)

    def test_create_user_rejects_invalid_input(self):
        admin = self.insert_admin()
        before = self.user_count()

        cases = (
            ("", "secret"),
            (" " * 3, "secret"),
            ("a" * (MAX_USERNAME_LENGTH + 1), "secret"),
            ("bob", ""),
        )

        for username, password in cases:
            with self.subTest(username=username, password=password):
                with self.assertRaises(ValueError):
                    create_user(self.connection, admin, username, password, "student")

        self.assertEqual(self.user_count(), before)

    def test_create_user_rejects_duplicate_username(self):
        admin = self.insert_admin()
        original = register_student(self.connection, "alice", "first password")
        before = self.user_count()

        with self.assertRaises(UsernameTakenError) as raised:
            create_user(self.connection, admin, "alice", "second password", "teacher")

        self.assertEqual(self.user_count(), before)
        self.assertEqual(self.role_of(original.id), "student")
        self.assertNotIn("second password", str(raised.exception))
        row = self.connection.execute(
            "SELECT password_hash FROM users WHERE id = ?", (original.id,)
        ).fetchone()
        self.assertTrue(verify_password("first password", row["password_hash"]))

    def test_change_user_role_requires_admin_actor(self):
        admin = self.insert_admin()
        student = self.insert_user("alice", "student")
        teacher = self.insert_user("carol", "teacher")

        impostor = User(
            id=admin.id,
            username=admin.username,
            role="student",
            created_at=admin.created_at,
        )
        actors = (student, teacher, impostor, None, "admin", admin.id)

        for actor in actors:
            with self.subTest(actor=actor):
                with self.assertRaises(ForbiddenActionError):
                    change_user_role(self.connection, actor, student.id, "teacher")

        self.assertEqual(self.role_of(student.id), "student")

    def test_change_user_role_rejects_invalid_role(self):
        admin = self.insert_admin()
        student = self.insert_user("alice", "student")

        for role in ("owner", "", None, 1):
            with self.subTest(role=role):
                with self.assertRaises(InvalidRoleError):
                    change_user_role(self.connection, admin, student.id, role)

        self.assertEqual(self.role_of(student.id), "student")

    def test_change_user_role_rejects_missing_user(self):
        admin = self.insert_admin()

        with self.assertRaises(MissingUserError):
            change_user_role(self.connection, admin, uuid.uuid4().hex, "teacher")

    def test_change_user_role_changes_role(self):
        admin = self.insert_admin()
        student = self.insert_user("alice", "student")

        result = change_user_role(self.connection, admin, student.id, "teacher")

        self.assertEqual(result.id, student.id)
        self.assertEqual(result.role, "teacher")
        self.assertEqual(result.username, "alice")
        self.assertEqual(result.created_at, student.created_at)
        self.assertEqual(self.role_of(student.id), "teacher")
        self.assertNotIn("password", vars(result))

    def test_change_user_role_same_role_is_noop(self):
        admin = self.insert_admin()
        teacher = self.insert_user("carol", "teacher")
        class_id = self.insert_class(teacher.id)

        result = change_user_role(self.connection, admin, teacher.id, "teacher")

        self.assertEqual(result.role, "teacher")
        self.assertEqual(result.created_at, teacher.created_at)
        self.assertEqual(self.role_of(teacher.id), "teacher")
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT id FROM classes WHERE id = ?", (class_id,)
            ).fetchone()
        )

    def test_change_user_role_rejects_teacher_owning_class(self):
        admin = self.insert_admin()
        teacher = self.insert_user("carol", "teacher")
        self.insert_class(teacher.id)

        with self.assertRaises(RoleConflictError) as raised:
            change_user_role(self.connection, admin, teacher.id, "student")

        self.assertEqual(self.role_of(teacher.id), "teacher")
        self.assertNotIn("password", str(raised.exception))

    def test_change_user_role_rejects_student_with_membership(self):
        admin = self.insert_admin()
        teacher = self.insert_user("carol", "teacher")
        student = self.insert_user("alice", "student")
        class_id = self.insert_class(teacher.id)
        self.insert_membership(student.id, class_id)

        with self.assertRaises(RoleConflictError):
            change_user_role(self.connection, admin, student.id, "teacher")

        self.assertEqual(self.role_of(student.id), "student")

    def test_change_user_role_allows_teacher_without_class(self):
        admin = self.insert_admin()
        teacher = self.insert_user("carol", "teacher")

        result = change_user_role(self.connection, admin, teacher.id, "admin")

        self.assertEqual(result.role, "admin")
        self.assertEqual(self.role_of(teacher.id), "admin")

    def test_change_user_role_allows_student_without_membership(self):
        admin = self.insert_admin()
        student = self.insert_user("alice", "student")

        result = change_user_role(self.connection, admin, student.id, "teacher")

        self.assertEqual(result.role, "teacher")
        self.assertEqual(self.role_of(student.id), "teacher")

    def test_exceptions_do_not_expose_hashes(self):
        admin = self.insert_admin()
        register_student(self.connection, "alice", "first password")
        student = self.connection.execute(
            "SELECT * FROM users WHERE username = 'alice'"
        ).fetchone()

        with self.assertRaises(UsernameTakenError) as raised:
            create_user(self.connection, admin, "alice", "first password", "student")

        self.assertNotIn(student["password_hash"], str(raised.exception))
        self.assertNotIn("first password", str(raised.exception))

    def test_list_users_is_bounded_ordered_and_admin_only(self):
        admin = self.insert_admin()
        for index in range(5):
            self.insert_user(f"user-{index}", "student")

        first = list_users(self.connection, admin, limit=2)
        self.assertEqual(len(first.items), 2)
        self.assertIsNotNone(first.next_key)
        for item in first.items:
            self.assertFalse(hasattr(item, "password_hash"))

        second = list_users(
            self.connection, admin, limit=2, after=first.next_key
        )
        ids = [item.id for item in first.items] + [
            item.id for item in second.items
        ]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))

        student = self.insert_user("student-actor", "student")
        with self.assertRaises(ForbiddenActionError):
            list_users(self.connection, student, limit=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
