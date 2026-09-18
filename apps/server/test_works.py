"""Tests for the bounded, portable file set a client may hand in.

A submission carries the student's own files, so this validator is the whole
trust boundary between a client body and everything downstream: only portable
``work/**`` paths survive, and the count, per-file size and total size are
bounded.  Each test names the exact reason code it expects, because callers
report that code and nothing else.
"""

import os
import sys
import unittest
from unittest import mock

# Allow ``import works`` no matter where the test is launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import works  # noqa: E402


class WorkFileValidationTest(unittest.TestCase):
    def test_client_cannot_submit_playground_or_server_paths(self):
        forbidden = [
            (".picopg/work.json", works.REASON_PATH_NOT_ALLOWED),
            (".picopg/extra.json", works.REASON_PATH_NOT_ALLOWED),
            ("picopg.json", works.REASON_PATH_NOT_ALLOWED),
            ("course/module/app.py", works.REASON_PATH_NOT_ALLOWED),
            ("starter/setup.py", works.REASON_PATH_NOT_ALLOWED),
            ("work/../escape.py", works.REASON_PATH_DOT_SEGMENT),
            ("work/./same.py", works.REASON_PATH_DOT_SEGMENT),
            ("work//empty.py", works.REASON_PATH_EMPTY_SEGMENT),
            ("/work/absolute.py", works.REASON_PATH_ABSOLUTE),
            ("work\\windows.py", works.REASON_PATH_BACKSLASH),
            ("work", works.REASON_PATH_NOT_ALLOWED),
            ("Work/main.py", works.REASON_PATH_NOT_ALLOWED),
        ]
        for path, reason in forbidden:
            with self.subTest(path=path):
                with self.assertRaises(works.WorkValidationError) as caught:
                    works.validate_work_files(
                        [works.WorkEntry(path, b"x")]
                    )
                self.assertEqual(caught.exception.reason, reason)

    def test_non_portable_work_paths_are_rejected(self):
        cases = [
            ("work/CON.py", works.REASON_PATH_RESERVED_NAME),
            ("work/sub/LPT1.txt", works.REASON_PATH_RESERVED_NAME),
            ("work/.git/config", works.REASON_PATH_RESERVED_NAME),
            ("work/a:b.py", works.REASON_PATH_PORTABLE_CHARACTER),
            ("work/trailing.", works.REASON_PATH_PORTABLE_CHARACTER),
            ("work/trailing ", works.REASON_PATH_PORTABLE_CHARACTER),
        ]
        for path, reason in cases:
            with self.subTest(path=path):
                with self.assertRaises(works.WorkValidationError) as caught:
                    works.validate_work_files([works.WorkEntry(path, b"x")])
                self.assertEqual(caught.exception.reason, reason)
                self.assertIn(reason, works.REASON_CODES)

    def test_casefold_collision_in_work_files_is_rejected(self):
        entries = [
            works.WorkEntry("work/File.py", b"1"),
            works.WorkEntry("work/file.py", b"2"),
        ]
        with self.assertRaises(works.WorkValidationError) as caught:
            works.validate_work_files(entries)
        self.assertEqual(
            caught.exception.reason, works.REASON_PATH_CASE_COLLISION
        )
        self.assertIn(works.REASON_PATH_CASE_COLLISION, works.REASON_CODES)

    def test_portable_work_near_misses_are_valid(self):
        result = works.validate_work_files(
            [
                works.WorkEntry("work/console.py", b"1"),
                works.WorkEntry("work/com10.txt", b"2"),
                works.WorkEntry("work/a b.txt", b"3"),
                works.WorkEntry("work/caf\u00e9.py", b"4"),
            ]
        )
        self.assertIn("work/console.py", result)
        self.assertIn("work/com10.txt", result)

    def test_invalid_path_shapes_and_entry_types_are_rejected(self):
        cases = [
            (works.WorkEntry(None, b"x"), works.REASON_PATH_NOT_STR),
            (works.WorkEntry("work/a\x00.py", b"x"), works.REASON_PATH_NUL),
            (works.WorkEntry("work/a.py", "text"), works.REASON_ENTRY_DATA_NOT_BYTES),
            ("not an entry", works.REASON_INVALID_ENTRY),
        ]
        for entry, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(works.WorkValidationError) as caught:
                    works.validate_work_files([entry])
                self.assertEqual(caught.exception.reason, reason)

    def test_control_character_paths_are_rejected(self):
        # A path with a C0 control character or DEL must be rejected by the
        # domain validator itself, not later by the repository tree writer.
        for path in ("work/a\nb.py", "work/a\x01b.py", "work/a\x7fb.py"):
            with self.subTest(path=path):
                with self.assertRaises(works.WorkValidationError) as caught:
                    works.validate_work_files([works.WorkEntry(path, b"x")])
                self.assertEqual(
                    caught.exception.reason,
                    works.REASON_PATH_CONTROL_CHARACTER,
                )
        self.assertIn(works.REASON_PATH_CONTROL_CHARACTER, works.REASON_CODES)

    def test_lone_surrogate_path_is_rejected(self):
        # A lone surrogate is not UTF-8 encodable; it must never reach the
        # repository tree writer (which would raise UnicodeEncodeError).
        for path in ("work/a\ud800b", "work/\udfff", "work/x/\ud800.py"):
            with self.subTest(path=path):
                with self.assertRaises(works.WorkValidationError) as caught:
                    works.validate_work_files([works.WorkEntry(path, b"x")])
                self.assertEqual(
                    caught.exception.reason,
                    works.REASON_PATH_INVALID_UNICODE,
                )
        self.assertIn(works.REASON_PATH_INVALID_UNICODE, works.REASON_CODES)

    def test_duplicate_normalized_paths_are_rejected(self):
        with self.assertRaises(works.WorkValidationError) as caught:
            works.validate_work_files(
                [
                    works.WorkEntry("work/a.py", b"1"),
                    works.WorkEntry("work/a.py", b"2"),
                ]
            )
        self.assertEqual(caught.exception.reason, works.REASON_DUPLICATE_PATH)

    def test_entry_count_limit(self):
        with mock.patch.object(works, "MAX_WORK_ENTRY_COUNT", 1):
            with self.assertRaises(works.WorkValidationError) as caught:
                works.validate_work_files(
                    [
                        works.WorkEntry("work/a.py", b"1"),
                        works.WorkEntry("work/b.py", b"2"),
                    ]
                )
        self.assertEqual(caught.exception.reason, works.REASON_TOO_MANY_ENTRIES)

    def test_per_file_size_limit(self):
        with mock.patch.object(works, "MAX_WORK_FILE_BYTES", 3):
            with self.assertRaises(works.WorkValidationError) as caught:
                works.validate_work_files(
                    [works.WorkEntry("work/a.py", b"1234")]
                )
        self.assertEqual(caught.exception.reason, works.REASON_FILE_TOO_LARGE)

    def test_total_work_size_limit(self):
        with mock.patch.object(works, "MAX_WORK_TOTAL_BYTES", 3):
            with self.assertRaises(works.WorkValidationError) as caught:
                works.validate_work_files(
                    [
                        works.WorkEntry("work/a.py", b"12"),
                        works.WorkEntry("work/b.py", b"34"),
                    ]
                )
        self.assertEqual(caught.exception.reason, works.REASON_WORK_TOO_LARGE)

    def test_limits_are_named_and_bounded(self):
        self.assertIsInstance(works.MAX_WORK_ENTRY_COUNT, int)
        self.assertIsInstance(works.MAX_WORK_FILE_BYTES, int)
        self.assertIsInstance(works.MAX_WORK_TOTAL_BYTES, int)
        self.assertGreater(works.MAX_WORK_ENTRY_COUNT, 0)
        self.assertGreater(works.MAX_WORK_FILE_BYTES, 0)
        self.assertGreaterEqual(
            works.MAX_WORK_TOTAL_BYTES, works.MAX_WORK_FILE_BYTES
        )

    # ----- ownership and authorization ----------------------------------


if __name__ == "__main__":
    unittest.main(verbosity=2)
