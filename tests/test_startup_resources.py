"""Static resources are read once at startup and fail with the file at fault; transient read failures are not remembered."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from picopg.server.ai.agent import load_teaching
from picopg.server.ai.prompts_loader import load_prompt
from picopg.server.lessons.index import LessonIndex
from picopg.server.playgrounds.releases import PlaygroundRelease


class StartupResourceTest(unittest.TestCase):
    def test_a_broken_teaching_file_names_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "0-core.md").write_text("---\nwhen: always\n---\n核心\n", encoding="utf-8")
            assert [skill.name for skill in load_teaching(root)] == ["core"]
            for name, text in (("1-a.md", "没有头部"), ("2-b.md", "---\nwhen: sometimes\n---\nx"), ("3-c.md", "---\nwhen: ask (\n---\nx")):
                (root / name).write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, name):
                    load_teaching(root)
                (root / name).unlink()

    def test_a_prompt_version_is_a_name_not_a_path(self) -> None:
        assert load_prompt("follow-v5", "PICOPG_FOLLOW_PROMPT")
        for wrong in ("../../etc/passwd", "follow-v999"):
            with self.assertRaisesRegex(ValueError, "PICOPG_FOLLOW_PROMPT"):
                load_prompt(wrong, "PICOPG_FOLLOW_PROMPT")

    def test_a_failed_package_read_is_retried_and_a_missing_outline_is_remembered(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("course/hanoi.outline.json", json.dumps({"version": 1, "knowledge": {"version": 3}}))
        release = PlaygroundRelease("hanoi", "sha256:" + "0" * 64, "application/zip", "汉诺塔")

        class Packages:
            reads = 0
            fail = True

            def catalog(self):
                return (release,)

            def package(self, playground_id, version_id):
                Packages.reads += 1
                if Packages.fail:
                    raise OSError("disk is busy")
                return "application/zip", archive.getvalue()

        index = LessonIndex(Packages())
        assert index.knowledge() is None
        Packages.fail = False
        assert index.knowledge() == {"version": 3}, "the transient failure was not cached"
        reads = Packages.reads
        index.knowledge()
        assert Packages.reads == reads, "a good outline is read once"


if __name__ == "__main__":
    unittest.main()
