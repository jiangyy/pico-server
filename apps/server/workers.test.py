from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for entry in (ROOT, SRC):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from apps.server.authentication.model import PrincipalSession  # noqa: E402
from apps.server.draft.protocol import decision_log_id, request_log_id, revision_log_id  # noqa: E402
from apps.server.draft.worker import DraftRequestConsumer  # noqa: E402
from apps.server.submission.logs import result_log as submission_result_log  # noqa: E402
from apps.server.submission.worker import SubmissionRequestConsumer  # noqa: E402
from picopg.api import Position, ReplicatedRecord, record_id_of  # noqa: E402
from picopg.impl.file_replica_log import FileReplicaLogStore  # noqa: E402


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


def replica_grants_are_fenced() -> None:
    PrincipalSession.create("user", "A", "token", {
        "principal/user/draft/lesson/revision/A": "append",
        "principal/user/draft/lesson/request/A": "append",
        "principal/user/submission/A/requests": "append",
        "principal/user/submission/A/results": "read",
    })
    try:
        PrincipalSession.create("user", "A", "token", {"principal/user/draft/lesson/request/B": "append"})
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
        branch_revisions_can_be_submitted(Path(raw))
    replica_grants_are_fenced()
    assert submission_result_log("principal/user/submission/A/requests") == "principal/user/submission/A/results"
    print("durable Draft rejection, replica fencing and Submission result routing passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
