"""Exhaustive tests for the pure Playground bundle validator."""

import dataclasses
import io
import json
import stat
import unittest
import warnings
import zipfile
from unittest import mock

import bundles
from bundles import (
    BundleEntry,
    BundleValidationError,
    MAX_ENTRY_COUNT,
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    REASON_CODES,
    SUPPORTED_RUNTIMES,
    SUPPORTED_SCHEMA_VERSIONS,
    decode_bundle_zip,
    validate_bundle,
)

MANIFEST_BYTES = b'{"schemaVersion":1,"runtime":"python"}'


def manifest_entry(data: bytes = MANIFEST_BYTES) -> BundleEntry:
    return BundleEntry("picopg.json", data)


def valid_bundle(*extra: BundleEntry) -> list[BundleEntry]:
    return [manifest_entry(), *extra]


class BundleTestCase(unittest.TestCase):
    def assert_reason(self, reason: str, entries) -> None:
        with self.assertRaises(BundleValidationError) as ctx:
            validate_bundle(entries)
        self.assertEqual(ctx.exception.reason, reason)
        self.assertIn(reason, REASON_CODES)

    def assert_reason_for_path(self, reason: str, path) -> None:
        self.assert_reason(reason, valid_bundle(BundleEntry(path, b"x")))


class BundleEntryTests(BundleTestCase):
    def test_entry_is_frozen(self):
        entry = BundleEntry("course/a.py", b"x")
        self.assertEqual(entry.kind, "file")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            entry.path = "course/b.py"  # type: ignore[misc]

    def test_entry_equality_and_hash(self):
        left = BundleEntry("course/a.py", b"x")
        right = BundleEntry("course/a.py", b"x")
        self.assertEqual(left, right)
        self.assertEqual(hash(left), hash(right))


class BundleShapeTests(BundleTestCase):
    def test_non_iterable_bundle(self):
        for bad in (None, 5, 3.14, object(), manifest_entry()):
            self.assert_reason(bundles.REASON_BUNDLE_NOT_ITERABLE, bad)

    def test_non_entry_member(self):
        for bad in (("course/a.py", b"x"), {"path": "course/a.py"}, "course/a.py", 7):
            self.assert_reason(bundles.REASON_INVALID_ENTRY, valid_bundle(bad))

    def test_bare_string_bundle_is_not_valid(self):
        self.assert_reason(bundles.REASON_INVALID_ENTRY, "picopg.json")

    def test_empty_bundle_missing_manifest(self):
        self.assert_reason(bundles.REASON_MISSING_MANIFEST, [])

    def test_generator_bundle_is_accepted(self):
        entries = valid_bundle(BundleEntry("course/a.py", b"x"))
        result = validate_bundle(entry for entry in entries)
        self.assertEqual(result["course/a.py"], b"x")


class ValidBundleTests(BundleTestCase):
    def test_minimal_bundle(self):
        result = validate_bundle(
            valid_bundle(
                BundleEntry("course/a.py", b"x"),
                BundleEntry("starter/b.py", b"y"),
            )
        )
        self.assertEqual(
            set(result), {"picopg.json", "course/a.py", "starter/b.py"}
        )
        self.assertEqual(result["picopg.json"], MANIFEST_BYTES)
        self.assertEqual(result["course/a.py"], b"x")
        self.assertEqual(result["starter/b.py"], b"y")
        self.assertIsInstance(result["course/a.py"], bytes)

    def test_manifest_only_bundle(self):
        result = validate_bundle([manifest_entry()])
        self.assertEqual(set(result), {"picopg.json"})

    def test_nested_course_and_starter_paths(self):
        result = validate_bundle(
            valid_bundle(
                BundleEntry("course/sub/dir/a.py", b"1"),
                BundleEntry("starter/sub/b.py", b"2"),
                BundleEntry("course/work/a.py", b"3"),
                BundleEntry("course/.picopg/a.py", b"4"),
            )
        )
        self.assertIn("course/sub/dir/a.py", result)
        self.assertIn("starter/sub/b.py", result)
        self.assertIn("course/work/a.py", result)
        self.assertIn("course/.picopg/a.py", result)

    def test_result_mapping_is_immutable(self):
        result = validate_bundle(valid_bundle(BundleEntry("course/a.py", b"x")))
        with self.assertRaises(TypeError):
            result["course/a.py"] = b"z"  # type: ignore[index]
        with self.assertRaises(TypeError):
            del result["course/a.py"]  # type: ignore[attr-defined]

    def test_iteration_order_does_not_change_result(self):
        entries = valid_bundle(
            BundleEntry("starter/b.py", b"2"),
            BundleEntry("course/a.py", b"1"),
            BundleEntry("course/sub/c.py", b"3"),
        )
        forward = validate_bundle(entries)
        backward = validate_bundle(list(reversed(entries)))
        shuffled = validate_bundle(
            [entries[2], entries[0], entries[1], entries[3]]
        )
        self.assertEqual(forward, backward)
        self.assertEqual(forward, shuffled)
        # Deterministic order, not just equal mappings.
        self.assertEqual(list(forward), list(backward))
        self.assertEqual(list(forward), list(shuffled))

    def test_normalizes_unicode_once(self):
        composed = "course/caf\u00e9.py"
        decomposed = "course/cafe\u0301.py"
        result = validate_bundle(valid_bundle(BundleEntry(decomposed, b"x")))
        self.assertIn(composed, result)
        self.assertNotIn(decomposed, result)


class PathRejectionTests(BundleTestCase):
    def test_absolute_paths(self):
        for path in ("/course/a.py", "//course/a.py", "/", "//", "/picopg.json"):
            self.assert_reason_for_path(bundles.REASON_PATH_ABSOLUTE, path)

    def test_backslashes(self):
        for path in ("course\\a.py", "course/a\\b.py", "..\\picopg.json"):
            self.assert_reason_for_path(bundles.REASON_PATH_BACKSLASH, path)

    def test_empty_segments(self):
        for path in ("", "course/", "course//a.py", "course/a.py/"):
            self.assert_reason_for_path(bundles.REASON_PATH_EMPTY_SEGMENT, path)

    def test_dot_segments(self):
        for path in (".", "..", "course/.", "course/..", "course/./a.py", "course/../starter/a.py", "./picopg.json"):
            self.assert_reason_for_path(bundles.REASON_PATH_DOT_SEGMENT, path)

    def test_nul_bytes(self):
        for path in ("course/\x00a.py", "picopg.json\x00", "\x00"):
            self.assert_reason_for_path(bundles.REASON_PATH_NUL, path)

    def test_non_string_paths(self):
        for path in (b"course/a.py", 1, None, ["course/a.py"]):
            self.assert_reason_for_path(bundles.REASON_PATH_NOT_STR, path)

    def test_forbidden_root_prefixes(self):
        for path in (".picopg", ".picopg/work.json", "work", "work/main.py", "work/sub/x.py"):
            self.assert_reason_for_path(bundles.REASON_PATH_FORBIDDEN, path)

    def test_paths_outside_allowlist(self):
        for path in (
            "picopg.JSON",
            "PICOPG.JSON",
            "course",
            "starter",
            "coursex/a.py",
            "courses/a.py",
            "other/a.py",
            "docs/a.py",
            "src/a.py",
            "C:/course/a.py",
            "course.txt",
        ):
            self.assert_reason_for_path(bundles.REASON_PATH_NOT_ALLOWED, path)

    def test_duplicate_exact_path(self):
        self.assert_reason(
            bundles.REASON_DUPLICATE_PATH,
            valid_bundle(
                BundleEntry("course/a.py", b"1"),
                BundleEntry("course/a.py", b"2"),
            ),
        )

    def test_duplicate_after_unicode_normalization(self):
        composed = "course/caf\u00e9.py"
        decomposed = "course/cafe\u0301.py"
        self.assert_reason(
            bundles.REASON_DUPLICATE_PATH,
            valid_bundle(
                BundleEntry(composed, b"1"),
                BundleEntry(decomposed, b"2"),
            ),
        )

    def test_duplicate_picopg_json(self):
        self.assert_reason(
            bundles.REASON_DUPLICATE_PATH,
            [manifest_entry(), manifest_entry()],
        )


class EntryKindTests(BundleTestCase):
    def test_only_file_kind_is_allowed(self):
        for kind in (
            "link",
            "symlink",
            "directory",
            "dir",
            "device",
            "char",
            "block",
            "fifo",
            "socket",
            "regular",
            "FILE",
            b"file",
            1,
            None,
        ):
            self.assert_reason(
                bundles.REASON_ENTRY_KIND_NOT_FILE,
                valid_bundle(BundleEntry("course/a.py", b"x", kind=kind)),
            )

    def test_forged_equality_kind_is_rejected(self):
        class ForgedKind:
            def __eq__(self, other):
                return True

            def __ne__(self, other):
                return False

            def __hash__(self):
                return 0

        self.assert_reason(
            bundles.REASON_ENTRY_KIND_NOT_FILE,
            valid_bundle(BundleEntry("course/a.py", b"x", kind=ForgedKind())),
        )

    def test_string_subclass_kind_is_rejected(self):
        class KindStr(str):
            pass

        self.assert_reason(
            bundles.REASON_ENTRY_KIND_NOT_FILE,
            valid_bundle(BundleEntry("course/a.py", b"x", kind=KindStr("file"))),
        )


class EntryDataTests(BundleTestCase):
    def test_data_must_be_bytes(self):
        for data in (bytearray(b"x"), memoryview(b"x"), "x", None, 1, [120]):
            self.assert_reason(
                bundles.REASON_ENTRY_DATA_NOT_BYTES,
                valid_bundle(BundleEntry("course/a.py", data)),
            )


class ExactTypeBoundaryTests(BundleTestCase):
    def test_bundle_entry_subclass_is_rejected(self):
        class EvilEntry(BundleEntry):
            pass

        self.assert_reason(
            bundles.REASON_INVALID_ENTRY,
            valid_bundle(EvilEntry("course/a.py", b"x")),
        )

    def test_forged_class_object_is_rejected_without_field_access(self):
        class Pretender:
            def __init__(self):
                self.accessed = []

            @property
            def __class__(self):
                return BundleEntry

            def __getattr__(self, name):
                self.accessed.append(name)
                raise AttributeError(name)

        pretender = Pretender()
        self.assert_reason(bundles.REASON_INVALID_ENTRY, valid_bundle(pretender))
        self.assertEqual(pretender.accessed, [])

    def test_bytes_subclass_as_ordinary_data_is_rejected(self):
        calls = []

        class LyingBytes(bytes):
            def __len__(self):
                calls.append("__len__")
                return 0

            def decode(self, *args, **kwargs):
                calls.append("decode")
                return '{"schemaVersion":1,"runtime":"python"}'

        self.assert_reason(
            bundles.REASON_ENTRY_DATA_NOT_BYTES,
            valid_bundle(BundleEntry("course/a.py", LyingBytes(b"x"))),
        )
        self.assertEqual(calls, [])

    def test_bytes_subclass_as_manifest_is_rejected(self):
        calls = []

        class LyingBytes(bytes):
            def __len__(self):
                calls.append("__len__")
                return 0

            def decode(self, *args, **kwargs):
                calls.append("decode")
                return '{"schemaVersion":1,"runtime":"python"}'

        self.assert_reason(
            bundles.REASON_ENTRY_DATA_NOT_BYTES,
            [BundleEntry("picopg.json", LyingBytes(b"x"))],
        )
        self.assertEqual(calls, [])

    def test_str_subclass_paths_are_rejected_before_any_method(self):
        cases = {
            "nul": "course/\x00a.py",
            "backslash": "course\\a.py",
            "dotdot": "course/../starter/a.py",
            "absolute": "/course/a.py",
            "disallowed": "other/a.py",
            "valid_looking": "course/a.py",
        }
        for label, raw in cases.items():
            with self.subTest(label=label):
                calls = []

                class LyingStr(str):
                    def __contains__(self, item):
                        calls.append("__contains__")
                        return False

                    def startswith(self, *args, **kwargs):
                        calls.append("startswith")
                        return True

                    def split(self, *args, **kwargs):
                        calls.append("split")
                        return ["course", "a.py"]

                    def __eq__(self, other):
                        calls.append("__eq__")
                        return True

                    def __ne__(self, other):
                        calls.append("__ne__")
                        return False

                self.assert_reason(
                    bundles.REASON_PATH_NOT_STR,
                    valid_bundle(BundleEntry(LyingStr(raw), b"x")),
                )
                self.assertEqual(calls, [])


class LimitTests(BundleTestCase):
    def test_entry_count_exact_limit(self):
        entries = [manifest_entry()]
        entries.extend(
            BundleEntry(f"course/f{i}.py", b"") for i in range(MAX_ENTRY_COUNT - 1)
        )
        self.assertEqual(len(entries), MAX_ENTRY_COUNT)
        result = validate_bundle(entries)
        self.assertEqual(len(result), MAX_ENTRY_COUNT)

    def test_entry_count_one_over(self):
        entries = [manifest_entry()]
        entries.extend(
            BundleEntry(f"course/f{i}.py", b"") for i in range(MAX_ENTRY_COUNT)
        )
        self.assertEqual(len(entries), MAX_ENTRY_COUNT + 1)
        self.assert_reason(bundles.REASON_TOO_MANY_ENTRIES, entries)

    def test_file_size_exact_limit(self):
        entries = valid_bundle(
            BundleEntry("course/big.bin", b"\x00" * MAX_FILE_BYTES)
        )
        result = validate_bundle(entries)
        self.assertEqual(len(result["course/big.bin"]), MAX_FILE_BYTES)

    def test_file_size_one_over(self):
        entries = valid_bundle(
            BundleEntry("course/big.bin", b"\x00" * (MAX_FILE_BYTES + 1))
        )
        self.assert_reason(bundles.REASON_FILE_TOO_LARGE, entries)

    def _exact_total_bundle(self):
        remaining = MAX_TOTAL_BYTES - len(MANIFEST_BYTES)
        entries = [manifest_entry()]
        index = 0
        while remaining > MAX_FILE_BYTES:
            entries.append(BundleEntry(f"course/big{index}.bin", b"\x00" * MAX_FILE_BYTES))
            remaining -= MAX_FILE_BYTES
            index += 1
        entries.append(BundleEntry(f"course/big{index}.bin", b"\x00" * remaining))
        return entries

    def test_total_bytes_exact_limit(self):
        entries = self._exact_total_bundle()
        result = validate_bundle(entries)
        self.assertEqual(sum(len(value) for value in result.values()), MAX_TOTAL_BYTES)

    def test_total_bytes_one_over(self):
        entries = self._exact_total_bundle()
        entries.append(BundleEntry("course/extra.bin", b"\x00"))
        self.assert_reason(bundles.REASON_BUNDLE_TOO_LARGE, entries)


class ManifestTests(BundleTestCase):
    def test_supported_constants_are_narrow(self):
        self.assertEqual(SUPPORTED_SCHEMA_VERSIONS, frozenset({1}))
        self.assertEqual(SUPPORTED_RUNTIMES, frozenset({"python"}))

    def test_missing_manifest(self):
        self.assert_reason(
            bundles.REASON_MISSING_MANIFEST,
            [BundleEntry("course/a.py", b"x")],
        )

    def test_manifest_not_utf8(self):
        for raw in (b"\xff\xfe", b"\xc3\x28", b"\x80"):
            self.assert_reason(
                bundles.REASON_MANIFEST_NOT_UTF8, [manifest_entry(raw)]
            )

    def test_manifest_not_json(self):
        for raw in (
            b"",
            b"{",
            b"not json",
            b"\xef\xbb\xbf" + MANIFEST_BYTES,
            b'{"schemaVersion":1,"runtime":"python",}',
            b'{"schemaVersion":1,"runtime":"py\nthon"}',
            b'{"schemaVersion":NaN,"runtime":"python"}',
            b'{"schemaVersion":Infinity,"runtime":"python"}',
        ):
            self.assert_reason(bundles.REASON_MANIFEST_NOT_JSON, [manifest_entry(raw)])

    def test_manifest_not_object(self):
        for raw in (b"[]", b'"x"', b"1", b"1.0", b"true", b"false", b"null"):
            self.assert_reason(
                bundles.REASON_MANIFEST_NOT_OBJECT, [manifest_entry(raw)]
            )

    def test_missing_schema_version(self):
        self.assert_reason(
            bundles.REASON_MANIFEST_MISSING_SCHEMA_VERSION,
            [manifest_entry(b'{"runtime":"python"}')],
        )

    def test_missing_runtime(self):
        self.assert_reason(
            bundles.REASON_MANIFEST_MISSING_RUNTIME,
            [manifest_entry(b'{"schemaVersion":1}')],
        )

    def test_invalid_schema_version(self):
        raw_values = (
            "true",
            "false",
            "1.0",
            "0.0",
            '"1"',
            '"python"',
            "null",
            "0",
            "2",
            "-1",
            "[]",
            "{}",
        )
        for raw in raw_values:
            payload = ('{"schemaVersion":%s,"runtime":"python"}' % raw).encode()
            self.assert_reason(
                bundles.REASON_MANIFEST_INVALID_SCHEMA_VERSION,
                [manifest_entry(payload)],
            )

    def test_schema_version_is_integer_one(self):
        result = validate_bundle([manifest_entry()])
        self.assertIn("picopg.json", result)

    def test_invalid_runtime(self):
        raw_values = (
            '"Python"',
            '"PYTHON"',
            '"python3"',
            '"pyodide"',
            '"cpython"',
            '"python "',
            '" python"',
            '"py"',
            "1",
            "1.0",
            "true",
            "false",
            "null",
            "[]",
            "{}",
        )
        for raw in raw_values:
            payload = ('{"schemaVersion":1,"runtime":%s}' % raw).encode()
            self.assert_reason(
                bundles.REASON_MANIFEST_INVALID_RUNTIME,
                [manifest_entry(payload)],
            )

    def test_extra_keys_are_opaque_and_allowed(self):
        payload = (
            b'{"schemaVersion":1,"runtime":"python","name":"anything",'
            b'"nested":{"arbitrary":[1,2,{"deep":true}]}}'
        )
        result = validate_bundle([manifest_entry(payload)])
        self.assertEqual(result["picopg.json"], payload)

    def test_duplicate_json_keys_last_value_wins(self):
        payload = b'{"schemaVersion":2,"schemaVersion":1,"runtime":"python"}'
        self.assertIn("picopg.json", validate_bundle([manifest_entry(payload)]))
        payload = b'{"schemaVersion":1,"schemaVersion":2,"runtime":"python"}'
        self.assert_reason(
            bundles.REASON_MANIFEST_INVALID_SCHEMA_VERSION,
            [manifest_entry(payload)],
        )


class ExceptionChainTests(BundleTestCase):
    def assert_clean_failure(self, ctx, secret, reason):
        exc = ctx.exception
        self.assertEqual(exc.reason, reason)
        self.assertNotIn(secret, str(exc))
        self.assertNotIn(secret, repr(exc))
        self.assertNotIn(secret, repr(exc.args))
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.__context__)

    def test_invalid_utf8_secret_is_unreachable(self):
        secret = "SECRET-UTF8-SENTINEL"
        raw = b"\xff" + secret.encode("ascii") + b"\xfe"
        with self.assertRaises(BundleValidationError) as ctx:
            validate_bundle([manifest_entry(raw)])
        self.assert_clean_failure(
            ctx, secret, bundles.REASON_MANIFEST_NOT_UTF8
        )

    def test_malformed_json_secret_is_unreachable(self):
        secret = "SECRET-JSON-SENTINEL"
        raw = ("{" + secret + ":").encode("utf-8")
        with self.assertRaises(BundleValidationError) as ctx:
            validate_bundle([manifest_entry(raw)])
        self.assert_clean_failure(
            ctx, secret, bundles.REASON_MANIFEST_NOT_JSON
        )

    def test_non_iterable_typeerror_secret_is_unreachable(self):
        secret = "SECRET-TYPEERROR-SENTINEL"

        class ExplodingIterable:
            def __iter__(self):
                raise TypeError(secret)

        with self.assertRaises(BundleValidationError) as ctx:
            validate_bundle(ExplodingIterable())
        self.assert_clean_failure(
            ctx, secret, bundles.REASON_BUNDLE_NOT_ITERABLE
        )

    def test_iteration_typeerror_is_not_swallowed(self):
        secret = "SECRET-ITERATION-SENTINEL"

        class ExplodingIterator:
            def __iter__(self):
                return self

            def __next__(self):
                raise TypeError(secret)

        with self.assertRaises(TypeError) as ctx:
            validate_bundle(ExplodingIterator())
        self.assertNotIsInstance(ctx.exception, BundleValidationError)
        self.assertIn(secret, str(ctx.exception))


class PurityTests(BundleTestCase):
    def test_validation_never_opens_files(self):
        entries = valid_bundle(
            BundleEntry("course/a.py", b"x"),
            BundleEntry("starter/b.py", b"y"),
        )
        with mock.patch("builtins.open", side_effect=AssertionError("filesystem")):
            result = validate_bundle(entries)
        self.assertEqual(result["course/a.py"], b"x")

    def test_validation_failure_never_opens_files(self):
        with mock.patch("builtins.open", side_effect=AssertionError("filesystem")):
            self.assert_reason(
                bundles.REASON_MISSING_MANIFEST,
                [BundleEntry("course/a.py", b"x")],
            )

    def test_module_has_no_io_or_network_helpers(self):
        for name in (
            "os",
            "io",
            "subprocess",
            "socket",
            "sqlite3",
            "shutil",
            "pathlib",
            "tempfile",
            "urllib",
            "http",
            "git",
        ):
            self.assertNotIn(name, vars(bundles))

    def test_error_never_contains_entry_content(self):
        secret = b"SUPER-SECRET-MANIFEST-CONTENT"
        with self.assertRaises(BundleValidationError) as ctx:
            validate_bundle([manifest_entry(secret)])
        self.assertNotIn("SUPER-SECRET-MANIFEST-CONTENT", str(ctx.exception))
        self.assertNotIn("SUPER-SECRET-MANIFEST-CONTENT", repr(ctx.exception))
        self.assertEqual(ctx.exception.reason, bundles.REASON_MANIFEST_NOT_JSON)

    def test_error_never_contains_path_content(self):
        secret_path = "course/SUPER-SECRET-PATH\x00.py"
        with self.assertRaises(BundleValidationError) as ctx:
            validate_bundle(valid_bundle(BundleEntry(secret_path, b"x")))
        self.assertNotIn("SUPER-SECRET-PATH", str(ctx.exception))
        self.assertEqual(ctx.exception.reason, bundles.REASON_PATH_NUL)


class ReasonCodeTests(BundleTestCase):
    def test_reason_codes_are_unique_nonempty_strings(self):
        codes = [getattr(bundles, name) for name in vars(bundles) if name.startswith("REASON_") and name != "REASON_CODES"]
        self.assertEqual(len(codes), len(set(codes)))
        for code in codes:
            self.assertIsInstance(code, str)
            self.assertTrue(code)
            self.assertIn(code, REASON_CODES)

    def test_every_reason_exposed_without_content(self):
        self.assertEqual(
            {getattr(bundles, name) for name in vars(bundles) if name.startswith("REASON_") and name != "REASON_CODES"},
            set(REASON_CODES),
        )


class DecodeBundleZipTests(unittest.TestCase):
    """The bounded ZIP-to-entries bridge that feeds the pure validator."""

    VALID_ENTRIES = (
        ("picopg.json", MANIFEST_BYTES),
        ("course/module/app.py", b"print(1)"),
        ("starter/setup.py", b"setup"),
    )

    def build_zip(self, entries, compression=zipfile.ZIP_DEFLATED):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return buffer.getvalue()

    def build_zip_with_info(self, entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for info, data in entries:
                archive.writestr(info, data)
        return buffer.getvalue()

    def assert_archive_reason(self, reason, data):
        with self.assertRaises(bundles.BundleArchiveError) as ctx:
            decode_bundle_zip(data)
        self.assertEqual(ctx.exception.reason, reason)
        self.assertIn(reason, REASON_CODES)
        self.assertIsInstance(ctx.exception, BundleValidationError)
        # Raised outside the except scope: no archive-derived error is chained.
        self.assertIsNone(ctx.exception.__cause__)
        self.assertIsNone(ctx.exception.__context__)

    def set_central_directory_encryption_flag(self, data):
        signature = b"PK\x01\x02"
        patched = bytearray(data)
        index = patched.find(signature)
        while index != -1:
            patched[index + 8] |= 0x01
            index = patched.find(signature, index + 4)
        return bytes(patched)

    def test_valid_zip_decodes_and_validates(self):
        data = self.build_zip(self.VALID_ENTRIES)
        result = validate_bundle(decode_bundle_zip(data))
        self.assertEqual(
            set(result),
            {"picopg.json", "course/module/app.py", "starter/setup.py"},
        )
        self.assertEqual(result["course/module/app.py"], b"print(1)")

    def test_stored_and_deflated_are_supported(self):
        for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(compression=compression):
                data = self.build_zip(self.VALID_ENTRIES, compression)
                result = validate_bundle(decode_bundle_zip(data))
                self.assertEqual(result["starter/setup.py"], b"setup")

    def test_malformed_archives(self):
        for data in (b"", b"not a zip", b"PK\x03\x04" + b"\x00" * 20):
            with self.subTest(data=data[:8]):
                self.assert_archive_reason(
                    bundles.REASON_ARCHIVE_INVALID, data
                )

    def test_non_bytes_input(self):
        for data in ("text", bytearray(b"x"), memoryview(b"x"), None, 5):
            with self.subTest(data=data):
                self.assert_archive_reason(
                    bundles.REASON_ARCHIVE_INVALID, data
                )

    def test_oversized_compressed_request(self):
        data = self.build_zip(self.VALID_ENTRIES)
        with self.assertRaises(bundles.BundleArchiveError) as ctx:
            decode_bundle_zip(data, max_archive_bytes=4)
        self.assertEqual(
            ctx.exception.reason, bundles.REASON_ARCHIVE_TOO_LARGE
        )

    def test_directory_member(self):
        data = self.build_zip(
            (("picopg.json", MANIFEST_BYTES), ("course/", b""))
        )
        self.assert_archive_reason(bundles.REASON_ARCHIVE_DIRECTORY, data)

    def test_symlink_member(self):
        info = zipfile.ZipInfo("course/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        data = self.build_zip_with_info(((info, b"../../outside"),))
        self.assert_archive_reason(bundles.REASON_ARCHIVE_LINK, data)

    def test_non_regular_member(self):
        info = zipfile.ZipInfo("course/fifo")
        info.external_attr = (stat.S_IFIFO | 0o644) << 16
        data = self.build_zip_with_info(((info, b"x"),))
        self.assert_archive_reason(bundles.REASON_ARCHIVE_ENTRY_TYPE, data)

    def test_encrypted_member(self):
        data = self.set_central_directory_encryption_flag(
            self.build_zip(self.VALID_ENTRIES)
        )
        self.assert_archive_reason(bundles.REASON_ARCHIVE_ENCRYPTED, data)

    def test_unsupported_compression(self):
        data = self.build_zip(self.VALID_ENTRIES, zipfile.ZIP_BZIP2)
        self.assert_archive_reason(bundles.REASON_ARCHIVE_COMPRESSION, data)

    def test_duplicate_member_name(self):
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("picopg.json", MANIFEST_BYTES)
                archive.writestr("picopg.json", MANIFEST_BYTES)
        self.assert_archive_reason(
            bundles.REASON_ARCHIVE_DUPLICATE_NAME, buffer.getvalue()
        )

    def test_entry_count_limit(self):
        entries = [("picopg.json", MANIFEST_BYTES)]
        entries.extend(
            (f"course/f{index}.py", b"") for index in range(MAX_ENTRY_COUNT)
        )
        data = self.build_zip(entries)
        self.assert_archive_reason(
            bundles.REASON_ARCHIVE_TOO_MANY_ENTRIES, data
        )

    def test_declared_file_size_limit(self):
        data = self.build_zip(
            (
                ("picopg.json", MANIFEST_BYTES),
                ("course/big.bin", b"\x00" * (MAX_FILE_BYTES + 1)),
            ),
            zipfile.ZIP_STORED,
        )
        self.assert_archive_reason(
            bundles.REASON_ARCHIVE_FILE_TOO_LARGE, data
        )

    def test_total_size_limit(self):
        entries = [("picopg.json", MANIFEST_BYTES)]
        remaining = MAX_TOTAL_BYTES - len(MANIFEST_BYTES) + 1
        index = 0
        while remaining > 0:
            chunk = min(remaining, MAX_FILE_BYTES)
            entries.append((f"course/big{index}.bin", b"\x00" * chunk))
            remaining -= chunk
            index += 1
        data = self.build_zip(entries, zipfile.ZIP_STORED)
        self.assert_archive_reason(
            bundles.REASON_ARCHIVE_BUNDLE_TOO_LARGE, data
        )

    def test_suspicious_compression_ratio(self):
        data = self.build_zip(
            (
                ("picopg.json", MANIFEST_BYTES),
                ("course/zeros.bin", b"\x00" * 65536),
            )
        )
        self.assert_archive_reason(
            bundles.REASON_ARCHIVE_SIZE_RATIO, data
        )

    def test_small_high_ratio_member_is_not_suspicious(self):
        data = self.build_zip(
            (
                ("picopg.json", MANIFEST_BYTES),
                ("course/small.bin", b"\x00" * 128),
            )
        )
        result = validate_bundle(decode_bundle_zip(data))
        self.assertEqual(len(result["course/small.bin"]), 128)

    def test_traversal_is_rejected_by_validation(self):
        for path in ("../escape.py", "course/../../escape.py", "/course/a.py"):
            with self.subTest(path=path):
                data = self.build_zip(
                    (("picopg.json", MANIFEST_BYTES), (path, b"x"))
                )
                entries = decode_bundle_zip(data)
                with self.assertRaises(BundleValidationError) as ctx:
                    validate_bundle(entries)
                self.assertIn(
                    ctx.exception.reason,
                    {
                        bundles.REASON_PATH_DOT_SEGMENT,
                        bundles.REASON_PATH_ABSOLUTE,
                    },
                )

    def test_decoding_never_extracts_or_opens_files(self):
        data = self.build_zip(self.VALID_ENTRIES)
        with mock.patch.object(
            zipfile.ZipFile, "extract", side_effect=AssertionError("extract")
        ), mock.patch.object(
            zipfile.ZipFile,
            "extractall",
            side_effect=AssertionError("extractall"),
        ), mock.patch(
            "builtins.open", side_effect=AssertionError("filesystem")
        ):
            result = validate_bundle(decode_bundle_zip(data))
        self.assertEqual(result["course/module/app.py"], b"print(1)")

    def test_errors_never_contain_member_names(self):
        info = zipfile.ZipInfo("course/SECRET-MEMBER-NAME")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        data = self.build_zip_with_info(((info, b"SECRET-BODY"),))
        with self.assertRaises(bundles.BundleArchiveError) as ctx:
            decode_bundle_zip(data)
        self.assertNotIn("SECRET-MEMBER-NAME", str(ctx.exception))
        self.assertNotIn("SECRET-MEMBER-NAME", repr(ctx.exception))
        self.assertNotIn("SECRET-BODY", repr(ctx.exception))
        self.assertEqual(ctx.exception.reason, bundles.REASON_ARCHIVE_LINK)


if __name__ == "__main__":
    unittest.main()
