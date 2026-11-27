"""课程包的 requiresSubmission：从 picopg.json 读出，写进 Release 记录，课程目录交给前端；写错的包不收。"""

from __future__ import annotations

import io
import json
import unittest
import zipfile

from picopg.server.playgrounds.packages import PackageError, ZipPackageAdapter
from picopg.server.playgrounds.releases import PlaygroundRelease, _release


def _package(**manifest: object) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("picopg.json", json.dumps({"schemaVersion": 1, "runtime": "python", "module": "course/demo.js", **manifest}))
        archive.writestr("course/index.html", "course")
        archive.writestr("course/demo.js", "export const Root = () => null;")
    return output.getvalue()


RELEASE = {"type": "playground.released", "playgroundId": "demo", "versionId": "sha256:" + "0" * 64, "mediaType": "application/zip"}


class RequiresSubmissionTest(unittest.TestCase):
    def test_package_declares_it(self) -> None:
        adapter = ZipPackageAdapter()
        self.assertTrue(adapter.inspect(_package(requiresSubmission=True)).requires_submission)
        self.assertFalse(adapter.inspect(_package(requiresSubmission=False)).requires_submission)
        self.assertFalse(adapter.inspect(_package()).requires_submission, "an older package without the field takes no hand-ins")
        with self.assertRaises(PackageError):
            adapter.inspect(_package(requiresSubmission="yes"))

    def test_release_and_catalog_carry_it(self) -> None:
        release = _release({**RELEASE, "requiresSubmission": True})
        self.assertIsInstance(release, PlaygroundRelease)
        assert release is not None
        self.assertTrue(release.requires_submission)
        self.assertEqual(release.to_catalog_value()["requires_submission"], True)
        older = _release(RELEASE)
        assert older is not None
        self.assertFalse(older.requires_submission)
        self.assertNotIn("requires_submission", older.to_catalog_value())
        with self.assertRaises(PackageError):
            _release({**RELEASE, "requiresSubmission": 1})


if __name__ == "__main__":
    unittest.main()
