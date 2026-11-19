"""An explicit grant cannot make a Server-owned Log writable by a client."""

from __future__ import annotations

import unittest

from picopg.server.analysis import LearningAnalysisLogRule
from picopg.server.auth.model import AuthenticatedReplica, PrincipalLogAuthorization, PrincipalSession, StaticReplicaDirectory
from picopg.server.drafts.protocol import DraftLogRule
from picopg.server.playgrounds.releases import PlaygroundLogRule
from picopg.server.submissions.logs import SubmissionLogRule

RULES = (PlaygroundLogRule(), DraftLogRule(), SubmissionLogRule(), LearningAnalysisLogRule())


def authorization(grants: dict[str, str]) -> PrincipalLogAuthorization:
    sessions = [PrincipalSession.create("u1", "r1", "token", grants, ["drafts"])]
    return PrincipalLogAuthorization(sessions, StaticReplicaDirectory(sessions), rules=RULES)


class GrantTest(unittest.TestCase):
    def test_server_owned_logs_cannot_be_granted_for_append(self) -> None:
        for log_id in (
            "principal/u1/draft/d1/decision",
            "principal/u1/draft/d1/comment",
            "principal/u1/submission/r1/results",
            "principal/u1/analysis/results",
            "playground-releases",
            "principal/u1/draft/d1/revision/r2",
        ):
            with self.assertRaises(ValueError, msg=log_id):
                authorization({log_id: "append"})

    def test_own_logs_and_logs_outside_every_rule_can_still_be_granted(self) -> None:
        granted = authorization({"principal/u1/draft/d1/revision/r1": "append", "notes/shared": "append", "principal/u1/draft/d1/decision": "read"})
        identity = AuthenticatedReplica("u1", "r1")
        assert granted.access(identity, "notes/shared") == "append"
        assert granted.access(identity, "principal/u1/draft/d1/decision") == "read"


if __name__ == "__main__":
    unittest.main()
