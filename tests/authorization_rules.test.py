from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from apps.server.authentication.model import (  # noqa: E402
    AuthenticatedReplica,
    PrincipalLogAuthorization,
    PrincipalSession,
    StaticReplicaDirectory,
)
from apps.server.draft.protocol import DraftLogRule  # noqa: E402
from apps.server.playground.catalog import PlaygroundLogRule  # noqa: E402
from apps.server.submission.logs import SubmissionLogRule  # noqa: E402
from apps.server.learning_analysis.logs import LearningAnalysisLogRule  # noqa: E402


def main() -> int:
    sessions = (
        PrincipalSession.create(
            "user",
            "A",
            "token-a",
            {"explicit": "read"},
            ("playgrounds", "drafts", "submissions", "analysis"),
        ),
        PrincipalSession.create(
            "user",
            "B",
            "token-b",
            {},
            ("drafts",),
        ),
    )
    authorization = PrincipalLogAuthorization(
        sessions,
        StaticReplicaDirectory(sessions),
        rules=(
            PlaygroundLogRule(),
            DraftLogRule(),
            SubmissionLogRule(),
            LearningAnalysisLogRule(),
        ),
    )
    identity = AuthenticatedReplica("user", "A")

    expected = {
        "explicit": "read",
        "playground-releases": "read",
        "principal/user/draft/lesson/decision": "read",
        "principal/user/draft/lesson/comment": "read",
        "principal/user/draft/lesson/revision/A": "append",
        "principal/user/draft/lesson/revision/B": "read",
        "principal/user/draft/lesson/request/A": "append",
        "principal/user/draft/lesson/request/B": None,
        "principal/user/submission/A/requests": "append",
        "principal/user/submission/A/results": "read",
        "principal/user/submission/B/requests": None,
        "principal/user/analysis/A/evidence": "append",
        "principal/user/analysis/B/evidence": None,
        "principal/user/analysis/results": "read",
        "principal/other/draft/lesson/decision": None,
    }
    for log_id, access in expected.items():
        assert authorization.access(identity, log_id) == access, log_id

    assert authorization.has_access(identity)
    assert not authorization.has_access(AuthenticatedReplica("user", "missing"))
    try:
        PrincipalLogAuthorization(sessions, StaticReplicaDirectory(sessions))
    except ValueError as error:
        assert "have no Log rules" in str(error)
    else:
        raise AssertionError("capabilities without registered Log rules were accepted")
    print("registered Principal Log authorization rules passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
