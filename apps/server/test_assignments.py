"""Assignment：与 Playground 同一套发布机制的另一半。

这些测试直接驱动领域层（不经过 HTTP），检查的是"存了什么、拒绝了什么"——
发布三阶段里属于数据库与 Git 的那两半，以及取消发布。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Allow ``import assignments`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import assignments  # noqa: E402
import database  # noqa: E402
import repository  # noqa: E402
from database import open_database  # noqa: E402


class AssignmentTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        base = Path(self.temporary_directory.name)
        self.connection = open_database(base / "app.db")
        self.addCleanup(self.connection.close)
        self.repository = repository.open_repository(base / "picopg.git")

    def metadata(self):
        return repository.CommitMetadata(
            name="PicoPG Operator",
            email="operator@picopg.invalid",
            timestamp="2026-01-01T00:00:00.000000Z",
            message="Publish assignment",
        )

    def publish(self, assignment_id, markdown, base_version_id=None, publish_id=None):
        """One publish, the way the application层 sequences it."""

        row = assignments.require_publishable(
            self.connection, assignment_id, base_version_id
        )
        title = assignments.decode_markdown(markdown)
        prepared = repository.prepare_assignment_version(
            self.repository,
            assignment_id,
            markdown,
            self.metadata(),
            publish_id or f"publish-{assignment_id}-{len(markdown)}",
            assignments.observed_version(row),
        )
        with database.immediate_transaction(self.connection):
            commit_id = assignments.commit_prepared_assignment(
                self.connection, assignment_id, prepared, base_version_id, title
            )
        if not prepared.no_op:
            repository.converge_assignment_ref(self.repository, assignment_id, commit_id)
            repository.delete_pending_ref(self.repository, prepared.publish_id)
        return prepared, commit_id

    def current(self, assignment_id):
        return assignments.get_assignment(self.connection, assignment_id)

    # ----- 上传的形状 -----------------------------------------------------

    def test_the_first_line_becomes_the_title(self):
        self.assertEqual(
            assignments.decode_markdown(b"# \xe4\xbd\x9c\xe4\xb8\x9a\xe4\xb8\x80\nprint(1)\n"),
            "作业一",
        )
        # 多个空格、CRLF、后面还有内容都不影响
        self.assertEqual(assignments.decode_markdown(b"#\t  Spaced  \r\nrest\n"), "Spaced")

    def test_a_body_that_is_not_a_title_is_refused(self):
        for payload in (
            b"",
            b"no title\n",
            b"## second level\n",
            b"#\n",
            b"#    \n",
            b"# " + "a".encode() * 201 + b"\n",
            b"# title\n\xff\xfe",
            "标题没有井号".encode(),
        ):
            with self.subTest(payload=payload[:20]):
                with self.assertRaises(assignments.InvalidAssignmentError):
                    assignments.decode_markdown(payload)

    def test_an_oversized_body_is_refused(self):
        with self.assertRaises(assignments.InvalidAssignmentError):
            assignments.decode_markdown(b"# t\n" + b"x" * assignments.MAX_MARKDOWN_BYTES)

    # ----- 发布 -----------------------------------------------------------

    def test_publishing_creates_a_version_and_puts_it_in_the_catalog(self):
        _, commit_id = self.publish("hw-1", b"# \xe7\xac\xac\xe4\xb8\x80\xe9\xa2\x98\n")

        row = self.current("hw-1")
        self.assertEqual(row.state, "active")
        self.assertEqual(row.current_version_id, commit_id)
        self.assertEqual(row.title, "第一题")
        self.assertEqual(
            assignments.active_assignments(self.connection),
            [{"assignment_id": "hw-1", "current_version_id": commit_id, "title": "第一题"}],
        )
        # 版本内容真的在 Git 里，且读得到
        self.assertTrue(
            repository.commit_in_assignment_history(self.repository, "hw-1", commit_id)
        )

    def test_the_same_content_is_a_no_op(self):
        _, first = self.publish("hw-1", b"# t\n")
        prepared, second = self.publish("hw-1", b"# t\n", base_version_id=first)

        self.assertTrue(prepared.no_op)
        self.assertEqual(second, first)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"],
            1,
        )

    def test_a_stale_base_is_refused(self):
        _, first = self.publish("hw-1", b"# one\n")
        _, second = self.publish("hw-1", b"# two\n", base_version_id=first)
        self.assertNotEqual(second, first)

        with self.assertRaises(assignments.AssignmentBaseChangedError):
            self.publish("hw-1", b"# three\n", base_version_id=first)
        self.assertEqual(self.current("hw-1").current_version_id, second)

    def test_the_first_publish_must_not_name_a_base(self):
        with self.assertRaises(assignments.AssignmentBaseChangedError):
            self.publish("hw-1", b"# t\n", base_version_id="a" * 40)
        with self.assertRaises(assignments.InvalidAssignmentError):
            self.publish("hw-1", b"# t\n", base_version_id="HEAD")

    # ----- 取消发布与重新上线 ---------------------------------------------

    def test_unpublishing_takes_it_out_of_the_catalog_and_keeps_the_version(self):
        _, commit_id = self.publish("hw-1", b"# t\n")
        self.assertTrue(assignments.unpublish(self.connection, "hw-1"))

        self.assertEqual(self.current("hw-1").state, "stopped")
        self.assertEqual(assignments.active_assignments(self.connection), [])
        self.assertTrue(
            repository.commit_in_assignment_history(self.repository, "hw-1", commit_id),
            "取消发布不删除版本",
        )
        # 再来一次是 no_op，而且不存在的作业是另一回事
        self.assertFalse(assignments.unpublish(self.connection, "hw-1"))
        with self.assertRaises(assignments.MissingAssignmentError):
            assignments.unpublish(self.connection, "nope")

    def test_publishing_again_brings_it_back(self):
        _, first = self.publish("hw-1", b"# one\n")
        assignments.unpublish(self.connection, "hw-1")

        prepared, second = self.publish("hw-1", b"# two\n", base_version_id=first)
        self.assertFalse(prepared.no_op)
        row = self.current("hw-1")
        self.assertEqual(row.state, "active", "发布就是重新上线")
        self.assertEqual(row.title, "two")
        self.assertEqual(
            [entry["assignment_id"] for entry in assignments.active_assignments(self.connection)],
            ["hw-1"],
        )
        self.assertNotEqual(second, first)

    def test_publishing_the_same_content_after_unpublishing_keeps_the_version(self):
        _, commit_id = self.publish("hw-1", b"# t\n")
        assignments.unpublish(self.connection, "hw-1")
        prepared, again = self.publish("hw-1", b"# t\n", base_version_id=commit_id)

        self.assertTrue(prepared.no_op)
        self.assertEqual(again, commit_id)
        self.assertEqual(self.current("hw-1").state, "active")


class AssignmentRecoveryTest(unittest.TestCase):
    """启动恢复要同时认得两套 ref：Playground 的与 Assignment 的。"""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        base = Path(self.temporary_directory.name)
        self.connection = open_database(base / "app.db")
        self.addCleanup(self.connection.close)
        self.repository = repository.open_repository(base / "picopg.git")

    def test_recovery_converges_the_assignment_ref(self):
        import playgrounds

        metadata = repository.CommitMetadata(
            name="PicoPG Operator",
            email="operator@picopg.invalid",
            timestamp="2026-01-01T00:00:00.000000Z",
            message="Publish assignment",
        )
        # 一次没有被数据库接受的发布：只有 pending ref，什么都不该留下
        dropped = repository.prepare_assignment_version(
            self.repository, "hw-1", b"# never\n", metadata, "publish-1"
        )
        dropped_count, converged = playgrounds.recover_pending_refs(
            self.connection, self.repository
        )
        self.assertEqual((dropped_count, converged), (1, 0))
        self.assertEqual(repository.list_pending_refs(self.repository), [])

        # 一次被接受的发布：history ref 收敛到数据库说的当前版本
        prepared = repository.prepare_assignment_version(
            self.repository, "hw-2", b"# kept\n", metadata, "publish-2"
        )
        title = assignments.decode_markdown(b"# kept\n")
        with database.immediate_transaction(self.connection):
            assignments.commit_prepared_assignment(
                self.connection, "hw-2", prepared, None, title
            )
        dropped_count, converged = playgrounds.recover_pending_refs(
            self.connection, self.repository
        )
        self.assertEqual((dropped_count, converged), (1, 1))
        ref = self.repository.assignment_ref("hw-2")
        self.assertEqual(
            repository._resolve_head(self.repository, ref), prepared.commit_id
        )
