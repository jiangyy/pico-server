"""Tests for published Playground metadata.

A Playground row only records that a bundle was published under an opaque
identifier; the identifiers are the interesting part, because one reaches SQL
and a Git ref.  These tests run against a real SQLite database and a real bare
repository in a temporary directory.
"""

import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

# Allow ``import playgrounds`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bundles  # noqa: E402
import database  # noqa: E402
import playgrounds  # noqa: E402
import repository  # noqa: E402
import manifests  # noqa: E402


def repository_module_head(repo, playground_id):
    return manifests.current_playground_commit(repo, playground_id)
from database import open_database  # noqa: E402

MANIFEST_BYTES = b'{"schemaVersion":1,"runtime":"python"}'


class PlaygroundTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        base = Path(self.temporary_directory.name)
        self.connection = open_database(base / "app.db")
        self.addCleanup(self.connection.close)
        self.repository = repository.open_repository(base / "picopg.git")

    def bundle(self, body=b"print(1)"):
        return bundles.validate_bundle(
            [
                bundles.BundleEntry("picopg.json", MANIFEST_BYTES),
                bundles.BundleEntry("course/module/app.py", body),
            ]
        )

    def metadata(self):
        return repository.CommitMetadata(
            name="PicoPG Operator",
            email="operator@picopg.invalid",
            timestamp="2026-01-01T00:00:00.000000Z",
            message="Publish playground bundle",
        )

    # ----- identifiers ---------------------------------------------------

    def test_identifiers_are_validated_before_storage(self):
        for identifier in ("", "bad/id", "bad id", "a" * 129, None, 7):
            with self.subTest(identifier=identifier):
                with self.assertRaises(playgrounds.InvalidRequestError):
                    playgrounds.create_playground(self.connection, identifier)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) AS n FROM playgrounds"
            ).fetchone()["n"],
            0,
        )

    def test_creating_the_same_identifier_twice_conflicts(self):
        playgrounds.create_playground(self.connection, "pg-one")

        with self.assertRaises(playgrounds.PlaygroundConflictError):
            playgrounds.create_playground(self.connection, "pg-one")

    def test_a_created_playground_can_be_read_back(self):
        created = playgrounds.create_playground(self.connection, "pg-one")

        stored = playgrounds.get_playground(self.connection, "pg-one")
        self.assertEqual(stored, created)
        self.assertEqual(created.id, "pg-one")
        self.assertTrue(created.created_at)

    def test_an_unknown_playground_reads_as_none(self):
        self.assertIsNone(playgrounds.get_playground(self.connection, "pg-none"))

    # ----- publishing ----------------------------------------------------

    def test_publishing_an_unknown_playground_creates_it(self):
        # implementation.md §3（不存在的 Playground 由第一次发布带出来）：第一次发布就是它的诞生，没有可以作 base 的东西。
        result = playgrounds.publish_playground(
            self.connection,
            "pg-none",
            self.repository,
            self.bundle(),
            self.metadata(),
        )

        self.assertFalse(result.no_op)
        row = self.connection.execute(
            "SELECT state, current_version_id FROM playgrounds WHERE id = 'pg-none'"
        ).fetchone()
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["current_version_id"], result.commit_id)
        self.assertTrue(
            repository.commit_in_playground_history(
                self.repository, "pg-none", result.commit_id
            )
        )
        # 旧命名空间不再被写
        self.assertFalse(
            (self.repository.path / "refs" / "heads" / "playgrounds").exists()
        )

    def test_publishing_records_a_commit_and_a_no_op_republish(self):
        playgrounds.create_playground(self.connection, "pg-one")

        first = playgrounds.publish_playground(
            self.connection, "pg-one", self.repository, self.bundle(), self.metadata()
        )
        self.assertFalse(first.no_op)
        self.assertEqual(len(first.commit_id), 40)

        again = playgrounds.publish_playground(
            self.connection, "pg-one", self.repository, self.bundle(), self.metadata(),
            base_version_id=first.commit_id,
        )
        self.assertTrue(again.no_op)
        self.assertEqual(again.commit_id, first.commit_id)

    def test_a_new_bundle_appends_a_new_commit(self):
        playgrounds.create_playground(self.connection, "pg-one")

        first = playgrounds.publish_playground(
            self.connection, "pg-one", self.repository, self.bundle(b"print(1)"),
            self.metadata(),
        )
        second = playgrounds.publish_playground(
            self.connection, "pg-one", self.repository, self.bundle(b"print(2)"),
            self.metadata(),
            base_version_id=first.commit_id,
        )
        self.assertNotEqual(first.commit_id, second.commit_id)
        self.assertTrue(
            repository.commit_in_playground_history(
                self.repository, "pg-one", first.commit_id
            )
        )


class BackfillCurrentVersionsTest(PlaygroundTest):
    """`current_version_id` is what the catalog reads, so it must be complete.

    A row from before that column existed has no value; start-up fills it in
    from the ref once, instead of the catalog running a Git subprocess per row
    per request. A row that never stored one is *not* in the catalog, so this is
    the step that keeps an upgraded deployment from losing its courses.
    """

    def stored(self, playground_id):
        row = self.connection.execute(
            "SELECT current_version_id FROM playgrounds WHERE id = ?",
            (playground_id,),
        ).fetchone()
        return None if row is None else row["current_version_id"]

    def blank(self, playground_id):
        """Erase the column, as a v1 → v2 migration leaves it."""

        with self.connection:
            self.connection.execute(
                "UPDATE playgrounds SET current_version_id = '' WHERE id = ?",
                (playground_id,),
            )

    def unfilled(self, playground_id):
        """A registered row that predates the column."""

        playgrounds.create_playground(self.connection, playground_id)
        self.blank(playground_id)

    def test_a_row_with_a_readable_ref_is_filled_in(self):
        playgrounds.create_playground(self.connection, "pg")
        result = playgrounds.publish_playground(
            self.connection, "pg", self.repository, self.bundle(), self.metadata()
        )
        self.blank("pg")
        self.assertEqual(self.stored("pg"), "")

        filled = playgrounds.backfill_current_versions(
            self.connection,
            lambda playground_id: repository_module_head(self.repository, playground_id),
        )
        self.assertEqual(filled, 1)
        self.assertEqual(self.stored("pg"), result.commit_id)

    def test_a_row_with_no_ref_is_left_alone(self):
        self.unfilled("pg-never-published")
        filled = playgrounds.backfill_current_versions(
            self.connection, lambda playground_id: None
        )
        self.assertEqual(filled, 0)
        self.assertEqual(self.stored("pg-never-published"), "")

    def test_a_row_that_already_has_a_version_is_not_resolved_again(self):
        playgrounds.create_playground(self.connection, "pg")
        playgrounds.publish_playground(
            self.connection, "pg", self.repository, self.bundle(), self.metadata()
        )
        asked = []
        filled = playgrounds.backfill_current_versions(
            self.connection, lambda playground_id: asked.append(playground_id)
        )
        self.assertEqual(filled, 0)
        self.assertEqual(asked, [], "an already-filled row must cost nothing")

    def test_an_archived_row_is_not_resolved(self):
        self.unfilled("pg-archived")
        with self.connection:
            self.connection.execute(
                "UPDATE playgrounds SET state = 'stopped' WHERE id = ?",
                ("pg-archived",),
            )
        asked = []
        playgrounds.backfill_current_versions(
            self.connection, lambda playground_id: asked.append(playground_id)
        )
        self.assertEqual(asked, [], "a stopped row is not in the catalog anyway")

    def test_it_is_idempotent(self):
        self.unfilled("pg")
        def resolve(playground_id):
            return "a" * 40

        self.assertEqual(playgrounds.backfill_current_versions(self.connection, resolve), 1)
        self.assertEqual(playgrounds.backfill_current_versions(self.connection, resolve), 0)
        self.assertEqual(self.stored("pg"), "a" * 40)



class PublishPhasesTest(PlaygroundTest):
    """三阶段 Publish：Prepare 产出 pending ref，数据库决定，Finalize 收敛。

    每条用例测的是**某一步之后世界长什么样**，因为这三步之间正是崩溃能插进来的地方。
    不在这里的 setUp 建行：父类的用例会数行数。
    """

    def pending(self):
        return [publish_id for publish_id, _ in repository.list_pending_refs(self.repository)]

    def history_head(self, playground_id="pg"):
        ref = self.repository.history_ref(playground_id)
        completed = subprocess.run(
            ["git", "-C", str(self.repository.path), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True, text=True,
        )
        return completed.stdout.strip() or None

    def test_prepare_creates_a_pending_ref_and_no_history_yet(self):
        playgrounds.create_playground(self.connection, "pg")
        prepared = repository.publish_playground_prepare(
            self.repository, "pg", self.bundle(), self.metadata(), "evt-1"
        )
        self.assertFalse(prepared.no_op)
        self.assertEqual(prepared.publish_id, "evt-1")
        self.assertEqual(self.pending(), ["evt-1"], "prepared Version 靠它防 GC")
        self.assertIsNone(self.history_head(), "还没被接受，history ref 不该动")

    def test_a_no_op_prepares_nothing(self):
        playgrounds.create_playground(self.connection, "pg")
        first = repository.publish_playground_prepare(
            self.repository, "pg", self.bundle(), self.metadata(), "evt-1"
        )
        repository.converge_history_ref(self.repository, "pg", first.commit_id)
        repository.delete_pending_ref(self.repository, "evt-1")

        again = repository.publish_playground_prepare(
            self.repository, "pg", self.bundle(), self.metadata(), "evt-2"
        )
        self.assertTrue(again.no_op)
        self.assertIsNone(again.publish_id, "no_op 没有任何东西需要保护")
        self.assertEqual(again.commit_id, first.commit_id)
        self.assertEqual(self.pending(), [], "no_op 不产生 pending ref")

    def test_committing_makes_the_prepared_version_current(self):
        # Accepting a publish is exactly this one write: the Version the Git
        # phase prepared becomes the Playground's current Version, in the same
        # transaction that decided it may.
        playgrounds.create_playground(self.connection, "pg")
        prepared = repository.publish_playground_prepare(
            self.repository, "pg", self.bundle(), self.metadata(), "publish-1"
        )
        with database.immediate_transaction(self.connection):
            result = playgrounds.commit_prepared_version(self.connection, "pg", prepared, None)
        self.assertEqual(result.commit_id, prepared.commit_id)

        row = self.connection.execute(
            "SELECT current_version_id FROM playgrounds WHERE id = 'pg'"
        ).fetchone()
        self.assertEqual(row["current_version_id"], prepared.commit_id)

    def test_committing_refuses_a_base_that_moved(self):
        playgrounds.create_playground(self.connection, "pg")
        first = repository.publish_playground_prepare(
            self.repository, "pg", self.bundle(b"one"), self.metadata(), "evt-1"
        )
        with database.immediate_transaction(self.connection):
            playgrounds.commit_prepared_version(self.connection, "pg", first, None)

        second = repository.publish_playground_prepare(
            self.repository, "pg", self.bundle(b"two"), self.metadata(), "evt-2"
        )
        with self.assertRaises(playgrounds.PlaygroundBaseChangedError):
            with database.immediate_transaction(self.connection):
                playgrounds.commit_prepared_version(self.connection, "pg", second, "a" * 40)


class PublishCrashRecoveryTest(PlaygroundTest):
    """启动时的恢复：pending ref 是唯一会被中断留下的痕迹。"""

    def pending(self):
        return [publish_id for publish_id, _ in repository.list_pending_refs(self.repository)]

    def history_head(self, playground_id="pg"):
        ref = self.repository.history_ref(playground_id)
        completed = subprocess.run(
            ["git", "-C", str(self.repository.path), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True, text=True,
        )
        return completed.stdout.strip() or None

    def test_prepare_then_crash_drops_the_ref_and_leaves_history_alone(self):
        # Git Prepare 成功、数据库事务没有提交：这个 Version 从没被接受。
        playgrounds.create_playground(self.connection, "pg")
        prepared = repository.publish_playground_prepare(
            self.repository, "pg", self.bundle(), self.metadata(), "evt-crash"
        )
        self.assertEqual(self.pending(), ["evt-crash"])

        dropped, converged = playgrounds.recover_pending_refs(self.connection, self.repository)

        self.assertEqual((dropped, converged), (1, 0))
        self.assertEqual(self.pending(), [], "未被接受的 pending ref 必须消失")
        self.assertIsNone(self.history_head(), "没被接受过就不该出现在 history 里")
        self.assertFalse(
            repository.commit_in_playground_history(self.repository, "pg", prepared.commit_id),
            "清理之后那个 commit 不再可见",
        )

    def test_commit_then_crash_converges_history_to_the_database(self):
        # 数据库提交了、Finalize 之前崩溃：Version 已被接受，history ref 落后。
        playgrounds.create_playground(self.connection, "pg")
        prepared = repository.publish_playground_prepare(
            self.repository, "pg", self.bundle(), self.metadata(), "evt-crash"
        )
        with database.immediate_transaction(self.connection):
            playgrounds.commit_prepared_version(self.connection, "pg", prepared, None)
        self.assertIsNone(self.history_head(), "Finalize 没跑，ref 还落后")
        self.assertEqual(self.pending(), ["evt-crash"])

        dropped, converged = playgrounds.recover_pending_refs(self.connection, self.repository)

        self.assertEqual((dropped, converged), (1, 1))
        self.assertEqual(self.history_head(), prepared.commit_id, "收敛到数据库说的当前版本")
        self.assertEqual(self.pending(), [])
        self.assertTrue(
            repository.commit_in_playground_history(self.repository, "pg", prepared.commit_id)
        )

    def test_recovery_is_idempotent(self):
        playgrounds.create_playground(self.connection, "pg")
        prepared = repository.publish_playground_prepare(
            self.repository, "pg", self.bundle(), self.metadata(), "evt-crash"
        )
        with database.immediate_transaction(self.connection):
            playgrounds.commit_prepared_version(self.connection, "pg", prepared, None)

        first = playgrounds.recover_pending_refs(self.connection, self.repository)
        head_after_first = self.history_head()
        second = playgrounds.recover_pending_refs(self.connection, self.repository)

        self.assertEqual(first, (1, 1))
        self.assertEqual(second, (0, 0), "第二次没有东西可做")
        self.assertEqual(self.history_head(), head_after_first)

    def test_recovery_drops_a_pending_ref_the_database_never_accepted(self):
        # 判据只有一条：数据库里有没有哪个 Playground 把这个 Version 当成当前版本。
        # 一个没有被接受的 commit（这里它来自另一次发布，数据库把别的 commit 记成了
        # 当前版本）只能丢掉，而且不能碰 history。
        playgrounds.create_playground(self.connection, "pg")
        with database.immediate_transaction(self.connection):
            playgrounds.commit_prepared_version(
                self.connection,
                "pg",
                repository.publish_playground_prepare(
                    self.repository, "pg", self.bundle(b"accepted"), self.metadata(), "publish-1"
                ),
                None,
            )
        head = self.history_head()
        # Prepare 自己就留下了 pending ref：那次发布没有被提交。
        repository.publish_playground_prepare(
            self.repository, "pg", self.bundle(b"never"), self.metadata(), "publish-2"
        )

        dropped, converged = playgrounds.recover_pending_refs(self.connection, self.repository)

        # 两个 pending ref：已接受那次（Finalize 还没删）收敛，没接受那次丢掉；
        # history 收敛到的仍然是数据库说的当前版本，也就是已接受的那一个。
        self.assertEqual((dropped, converged), (2, 1))
        self.assertEqual(self.pending(), [])
        self.assertNotEqual(self.history_head(), head, "history 收敛到数据库的当前版本")

    def test_recovery_converges_to_current_not_to_the_pending_commit(self):
        # 关键：pending ref 指向那次尝试的 commit，但数据库可能已经被后续发布推得
        # 更远。收敛的目标永远是数据库，不能把 ref 拉回去。
        playgrounds.create_playground(self.connection, "pg")
        first = repository.publish_playground_prepare(
            self.repository, "pg", self.bundle(b"one"), self.metadata(), "evt-1"
        )
        with database.immediate_transaction(self.connection):
            playgrounds.commit_prepared_version(self.connection, "pg", first, None)

        second = repository.publish_playground_prepare(
            self.repository, "pg", self.bundle(b"two"), self.metadata(), "evt-2"
        )
        with database.immediate_transaction(self.connection):
            playgrounds.commit_prepared_version(self.connection, "pg", second, first.commit_id)
        repository.converge_history_ref(self.repository, "pg", second.commit_id)
        repository.delete_pending_ref(self.repository, "evt-2")

        playgrounds.recover_pending_refs(self.connection, self.repository)

        self.assertEqual(self.history_head(), second.commit_id, "不能被旧的 pending ref 拉回去")
        self.assertEqual(self.pending(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
