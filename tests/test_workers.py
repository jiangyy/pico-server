from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for entry in (ROOT, SRC):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from picopg.server.auth.model import (  # noqa: E402
    PrincipalLogAuthorization,
    PrincipalSession,
    StaticReplicaDirectory,
)
from picopg.server.drafts.protocol import (  # noqa: E402
    DraftLogRule,
    decision_log_id,
    request_log_id,
    revision_log_id,
)
from picopg.server.drafts.authority import DraftAuthority  # noqa: E402
from picopg.server.drafts.worker import DraftRequestConsumer  # noqa: E402
from picopg.server.playgrounds.releases import PlaygroundLogRule  # noqa: E402
from picopg.server.submissions.logs import (  # noqa: E402
    SubmissionLogRule,
    result_log as submission_result_log,
)
from picopg.server.submissions.worker import SubmissionRequestConsumer  # noqa: E402
from picopg.replica import Position, ReplicatedRecord, record_id_of  # noqa: E402
from picopg.replica.file_store import FileReplicaLogStore  # noqa: E402


def record(
    log_id: str,
    parent: Position | None,
    requires: tuple[Position, ...],
    data: dict[str, object],
) -> ReplicatedRecord:
    return ReplicatedRecord(record_id_of(log_id, parent, requires, data), log_id, parent, requires, data)


def append(store: FileReplicaLogStore, value: ReplicatedRecord) -> Position:
    result = store.append(value.log_id, value.parent, (value,))
    assert result.kind == "accepted"
    return value.position


def draft_rejections_are_durable(root: Path) -> None:
    store = FileReplicaLogStore(root)
    principal_id = "user"
    draft_id = "lesson"
    origin_log = revision_log_id(principal_id, draft_id, "origin")
    initial = append(store, record(origin_log, None, (), {
        "type": "draft.revised", "draftId": draft_id, "replicaId": "origin",
        "source": "print(0)", "base": None,
    }))
    requests: list[str] = []
    request_heads: dict[str, Position] = {}
    for replica_id in ("A", "B"):
        log_id = request_log_id(principal_id, draft_id, replica_id)
        requests.append(log_id)
        request_heads[replica_id] = append(store, record(log_id, None, (initial,), {
            "type": "draft.beginEditing", "commandId": f"begin-{replica_id}",
            "draftId": draft_id, "replicaId": replica_id,
            "expectedHead": initial.to_value(), "expectedEpoch": 0,
        }))

    consumer = DraftRequestConsumer(store, tuple(requests))
    outcomes = consumer.run_once()
    assert [result.kind for _, result in outcomes] == ["accepted", "activeElsewhere"]
    decisions = store.read(decision_log_id(principal_id, draft_id), None, 10).records
    assert [decision.data["type"] for decision in decisions] == [
        "draft.started", "draft.editingStarted", "draft.rejected",
    ]
    assert decisions[-1].data["reason"] == "activeElsewhere"

    b_revision = append(store, record(revision_log_id(principal_id, draft_id, "B"), None, (initial,), {
        "type": "draft.revised", "draftId": draft_id, "replicaId": "B",
        "source": "print('B')", "base": initial.to_value(),
    }))
    append(store, record(request_log_id(principal_id, draft_id, "A"), request_heads["A"], (initial, b_revision), {
        "type": "draft.takeover", "commandId": "foreign-target",
        "draftId": draft_id, "replicaId": "A", "expectedHead": initial.to_value(),
        "expectedEpoch": 1, "targetHead": b_revision.to_value(),
    }))
    invalid = consumer.run_once()
    assert len(invalid) == 1 and invalid[0][1].kind == "invalid"
    decisions = store.read(decision_log_id(principal_id, draft_id), None, 10).records
    assert decisions[-1].data["type"] == "draft.rejected"
    assert decisions[-1].data["reason"] == "invalid"

    head = store.head(decision_log_id(principal_id, draft_id))
    replayed = DraftRequestConsumer(store, tuple(requests)).run_once()
    assert [result.kind for _, result in replayed] == ["current", "invalid", "activeElsewhere"]
    assert store.head(decision_log_id(principal_id, draft_id)) == head


def draft_transitions_and_faults_are_visible(root: Path) -> None:
    store = FileReplicaLogStore(root)
    authority = DraftAuthority(store)
    principal_id = "user"
    draft_id = "lesson"
    revision_a = revision_log_id(principal_id, draft_id, "A")
    request_a = request_log_id(principal_id, draft_id, "A")
    initial = append(store, record(revision_a, None, (), {
        "type": "draft.revised", "draftId": draft_id, "replicaId": "A",
        "source": "print(0)", "base": None,
    }))
    begin = append(store, record(request_a, None, (initial,), {
        "type": "draft.beginEditing", "commandId": "begin-A",
        "draftId": draft_id, "replicaId": "A",
        "expectedHead": initial.to_value(), "expectedEpoch": 0,
    }))
    assert authority.handle(begin).kind == "accepted"

    advanced = append(store, record(revision_a, initial, (), {
        "type": "draft.revised", "draftId": draft_id, "replicaId": "A",
        "source": "print(1)", "base": initial.to_value(),
    }))
    advance = append(store, record(request_a, begin, (initial, advanced), {
        "type": "draft.advance", "commandId": "advance-A",
        "draftId": draft_id, "replicaId": "A",
        "expectedHead": initial.to_value(), "expectedEpoch": 1,
        "targetHead": advanced.to_value(),
    }))
    advanced_result = authority.handle(advance)
    assert advanced_result.kind == "accepted"
    assert advanced_result.ref is not None and advanced_result.ref.head == advanced

    revision_b = revision_log_id(principal_id, draft_id, "B")
    request_b = request_log_id(principal_id, draft_id, "B")
    takeover_head = append(store, record(revision_b, None, (advanced,), {
        "type": "draft.revised", "draftId": draft_id, "replicaId": "B",
        "source": "print(2)", "base": advanced.to_value(),
    }))
    takeover = append(store, record(request_b, None, (advanced, takeover_head), {
        "type": "draft.takeover", "commandId": "takeover-B",
        "draftId": draft_id, "replicaId": "B",
        "expectedHead": advanced.to_value(), "expectedEpoch": 1,
        "targetHead": takeover_head.to_value(),
    }))
    takeover_result = authority.handle(takeover)
    assert takeover_result.kind == "accepted"
    assert takeover_result.ref is not None
    assert takeover_result.ref.writer == "B" and takeover_result.ref.epoch == 2
    replayed = authority.handle(takeover)
    assert replayed.kind == "current" and replayed.decision == takeover_result.decision

    decision_log = decision_log_id(principal_id, draft_id)
    decision_head = store.head(decision_log)
    assert decision_head is not None
    append(store, record(decision_log, decision_head, (), {
        "type": "broken", "draftId": draft_id,
        "commandId": "broken", "epoch": 2,
    }))
    next_request = append(store, record(request_b, takeover, (takeover_head,), {
        "type": "draft.beginEditing", "commandId": "after-broken",
        "draftId": draft_id, "replicaId": "B",
        "expectedHead": takeover_head.to_value(), "expectedEpoch": 2,
    }))
    try:
        authority.handle(next_request)
    except ValueError as error:
        assert str(error) == "decision type is invalid"
    else:
        raise AssertionError("invalid Decision Log was hidden as a command rejection")


def replica_grants_are_fenced() -> None:
    valid = PrincipalSession.create("user", "A", "token", {
        "principal/user/draft/lesson/revision/A": "append",
        "principal/user/draft/lesson/request/A": "append",
        "principal/user/submission/A/requests": "append",
        "principal/user/submission/A/results": "read",
    })
    rules = (PlaygroundLogRule(), DraftLogRule(), SubmissionLogRule())
    PrincipalLogAuthorization(
        (valid,),
        StaticReplicaDirectory((valid,)),
        rules=rules,
    )
    invalid = PrincipalSession.create(
        "user",
        "A",
        "token",
        {"principal/user/draft/lesson/request/B": "append"},
    )
    try:
        PrincipalLogAuthorization(
            (invalid,),
            StaticReplicaDirectory((invalid,)),
            rules=rules,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("another Replica received an append grant")


def branch_revisions_can_be_submitted(root: Path) -> None:
    class Tags:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, **values: object) -> str:
            self.calls += 1
            assert values["source"] == "print('branch')"
            return "picopg/submissions/branch"

    store = FileReplicaLogStore(root)
    revision = append(store, record("principal/user/draft/lesson/revision/A", None, (), {
        "type": "draft.revised", "draftId": "lesson", "replicaId": "A",
        "source": "print('branch')", "base": None,
    }))
    request_log = "principal/user/submission/A/requests"
    invalid = append(store, record(request_log, None, (revision,), {
        "type": "submission.requested", "submissionId": "broken",
        "playgroundId": "lesson", "revision": "not-a-position",
    }))
    request = append(store, record(request_log, invalid, (revision,), {
        "type": "submission.requested", "submissionId": "one",
        "playgroundId": "lesson", "revision": revision.to_value(),
    }))
    tags = Tags()
    worker = SubmissionRequestConsumer(store, (request_log,), tags)
    assert worker.run_once() == 2
    assert worker.run_once() == 0
    assert tags.calls == 1
    results = store.read("principal/user/submission/A/results", None, 10).records
    assert results[0].data["type"] == "submission.failed"
    assert results[0].data["request"] == invalid.to_value()
    result = results[1]
    assert result.data["type"] == "submission.completed"
    assert result.data["request"] == request.to_value()
    assert result.requires == (request, revision)


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        draft_rejections_are_durable(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        draft_transitions_and_faults_are_visible(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        branch_revisions_can_be_submitted(Path(raw))
    replica_grants_are_fenced()
    assert submission_result_log("principal/user/submission/A/requests") == "principal/user/submission/A/results"
    print("durable Draft rejection, replica fencing and Submission result routing passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
