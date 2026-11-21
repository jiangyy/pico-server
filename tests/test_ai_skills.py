from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from picopg.server.ai.course import CourseSkillRepository
from picopg.server.ai.model import CourseAiError
from picopg.server.playgrounds.packages import FilePackageStore, version_id


class CourseSkillRepositoryTest(unittest.TestCase):
    def test_the_skill_comes_from_the_course_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = FilePackageStore(root / "packages")
            output = BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("course/hanoi.skill.json", json.dumps({
                    "course": "hanoi", "actions": ["code.run"], "teaches": "包里的版本",
                }))
            payload = output.getvalue()
            packaged_version = version_id(payload)
            packages.put("hanoi", packaged_version, payload)
            repository = CourseSkillRepository(packages)
            self.assertEqual(repository.read("hanoi", packaged_version)["teaches"], "包里的版本")

            output = BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("course/other.json", "{}")
            payload = output.getvalue()
            legacy_version = version_id(payload)
            packages.put("hanoi", legacy_version, payload)
            with self.assertRaises(CourseAiError):
                repository.read("hanoi", legacy_version)
            with self.assertRaises(CourseAiError):
                repository.read("hanoi", None)


if __name__ == "__main__":
    unittest.main()
