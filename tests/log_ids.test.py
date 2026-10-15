from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from apps.server.draft.protocol import draft_scope, revision_log_id  # noqa: E402
from apps.server.log_ids import decode_component, encode_component  # noqa: E402
from apps.server.review.comments import comment_log_id  # noqa: E402
from apps.server.submission.logs import request_scope, result_log  # noqa: E402


def main() -> int:
    vectors = {
        "plain": "plain",
        "student'o /中文%": "student'o%20%2F%E4%B8%AD%E6%96%87%25",
        "-_.!~*'()": "-_.!~*'()",
    }
    for raw, encoded in vectors.items():
        assert encode_component(raw, "value") == encoded
        assert decode_component(encoded, "value") == raw

    principal = "student'o /中文%"
    draft = "lesson one"
    replica = "device/a"
    revision = revision_log_id(principal, draft, replica)
    assert draft_scope(revision, "revision") == (principal, draft, replica)
    assert comment_log_id(principal, draft).startswith(
        "principal/student'o%20%2F%E4%B8%AD%E6%96%87%25/"
    )

    request = (
        "principal/student'o%20%2F%E4%B8%AD%E6%96%87%25/"
        "submission/device%2Fa/requests"
    )
    assert request_scope(request) == (principal, replica)
    assert result_log(request).endswith("/submission/device%2Fa/results")

    for noncanonical in ("space here", "%2f", "%", "中文"):
        try:
            decode_component(noncanonical, "value")
        except ValueError as error:
            assert "canonically encoded" in str(error)
        else:
            raise AssertionError(f"non-canonical component was accepted: {noncanonical}")

    print("canonical Log ID component vectors passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
