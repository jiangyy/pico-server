"""Tests for bounded structured audit events."""

import io
import json
import unittest
from datetime import datetime, timedelta, timezone

import audit
from audit import AuditLog, sanitize


START = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)


class FakeClock:
    """Return successive instants a fixed step apart."""

    def __init__(self, start=START, step=timedelta(milliseconds=5)):
        self._current = start
        self._step = step

    def __call__(self):
        moment = self._current
        self._current = moment + self._step
        return moment


class SanitizeTest(unittest.TestCase):
    def test_control_characters_replaced_and_none_kept(self):
        self.assertIsNone(sanitize(None))
        self.assertEqual(sanitize("a\nb\tc\rd\x00e\x7f"), "a?b?c?d?e?")

    def test_field_is_bounded(self):
        self.assertEqual(len(sanitize("x" * 500)), audit.MAX_FIELD_LENGTH)
        self.assertEqual(len(sanitize("x" * 500, 10)), 10)

    def test_non_string_values_are_stringified(self):
        self.assertEqual(sanitize(123), "123")


class RequestIdTest(unittest.TestCase):
    def _log(self, generated="fallback"):
        return AuditLog(
            output=io.StringIO(),
            clock=FakeClock(),
            request_ids=lambda: generated,
        )

    def test_accepts_bounded_header_safe_id(self):
        log = self._log()
        for value in ("abc-123", "A.b_c-d", "x" * audit.MAX_REQUEST_ID_LENGTH):
            self.assertEqual(log.request_id(value), value)

    def test_replaces_unsafe_or_oversized_ids(self):
        log = self._log()
        for value in (
            "",
            "has space",
            "new\nline",
            "semi;colon",
            "slash/es",
            "x" * (audit.MAX_REQUEST_ID_LENGTH + 1),
            None,
            7,
        ):
            self.assertEqual(log.request_id(value), "fallback", value)

    def test_generated_id_is_sanitized_and_bounded(self):
        log = AuditLog(
            output=io.StringIO(),
            clock=FakeClock(),
            request_ids=lambda: "weird id\n" + "z" * 200,
        )
        value = log.request_id()
        self.assertLessEqual(len(value), audit.MAX_REQUEST_ID_LENGTH)
        self.assertNotIn(" ", value)
        self.assertNotIn("\n", value)

    def test_empty_generated_id_falls_back_to_uuid(self):
        log = AuditLog(
            output=io.StringIO(), clock=FakeClock(), request_ids=lambda: ""
        )
        self.assertTrue(log.request_id())

    def test_id_equal_to_secret_is_replaced(self):
        log = self._log()
        token = "abc_DEF-1234567890"

        # A token-shaped ID is otherwise acceptable, but must never echo the
        # request's own credential.
        self.assertEqual(log.request_id(token), token)
        self.assertEqual(log.request_id(token, secret=token), "fallback")
        self.assertEqual(
            log.request_id("safe-trace-id", secret=token), "safe-trace-id"
        )


class RecordTest(unittest.TestCase):
    def _record(self, output, **overrides):
        log = AuditLog(output=output, clock=FakeClock())
        values = {
            "request_id": "req-1",
            "actor_id": "actor-1",
            "action": "playground.publish",
            "target": "pg-1",
            "status": 201,
            "code": None,
            "started_at": log.now(),
        }
        values.update(overrides)
        log.record(**values)

    def test_event_has_exact_bounded_shape(self):
        output = io.StringIO()
        self._record(output)

        text = output.getvalue()
        self.assertEqual(text.count("\n"), 1)
        self.assertTrue(text.endswith("\n"))
        event = json.loads(text)
        self.assertEqual(
            set(event),
            {
                "time",
                "requestId",
                "actorId",
                "action",
                "target",
                "status",
                "code",
                "durationMs",
            },
        )
        self.assertEqual(event["time"], "2026-01-02T03:04:05.128456Z")
        self.assertEqual(event["requestId"], "req-1")
        self.assertEqual(event["actorId"], "actor-1")
        self.assertEqual(event["action"], "playground.publish")
        self.assertEqual(event["target"], "pg-1")
        self.assertEqual(event["status"], 201)
        self.assertIsNone(event["code"])
        self.assertEqual(event["durationMs"], 5.0)

    def test_missing_actor_and_code_are_null(self):
        output = io.StringIO()
        self._record(output, actor_id=None, target=None, code="forbidden")

        event = json.loads(output.getvalue())
        self.assertIsNone(event["actorId"])
        self.assertIsNone(event["target"])
        self.assertEqual(event["code"], "forbidden")

    def test_control_characters_never_break_the_line(self):
        output = io.StringIO()
        self._record(
            output,
            action="bad\nvalue",
            target="a\rb\tc",
            actor_id="x\x00y",
        )

        text = output.getvalue()
        self.assertEqual(text.count("\n"), 1)
        event = json.loads(text)
        self.assertNotIn("\n", event["action"])
        self.assertNotIn("\r", event["target"])
        self.assertNotIn("\t", event["target"])
        self.assertNotIn("\x00", event["actorId"])

    def test_unicode_line_separator_is_escaped(self):
        output = io.StringIO()
        self._record(output, action="a\u2028b")

        text = output.getvalue()
        self.assertEqual(text.count("\n"), 1)
        self.assertNotIn("\u2028", text)

    def test_oversized_fields_are_truncated(self):
        output = io.StringIO()
        self._record(
            output,
            actor_id="a" * 500,
            target="t" * 500,
            action="x" * 500,
        )

        event = json.loads(output.getvalue())
        self.assertEqual(len(event["actorId"]), audit.MAX_FIELD_LENGTH)
        self.assertEqual(len(event["target"]), audit.MAX_FIELD_LENGTH)
        self.assertEqual(len(event["action"]), audit.MAX_ACTION_LENGTH)

    def test_output_failure_is_swallowed(self):
        class BrokenSink:
            def write(self, line):
                raise OSError("audit sink down")

            def flush(self):
                raise OSError("audit sink down")

        # Must not raise even though every sink operation fails.
        self._record(BrokenSink())


if __name__ == "__main__":
    unittest.main(verbosity=2)
