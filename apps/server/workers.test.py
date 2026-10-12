from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for entry in (ROOT, SRC):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from apps.server.authorization import PrincipalSession  # noqa: E402
from apps.server.drafts import decision_log_id, request_log_id, revision_log_id  # noqa: E402
from apps.server.workers import (  # noqa: E402
    DraftRequestConsumer,
    LogWorker,
    SubmissionHandler,
    submission_result_log,
)
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
    draft_id = "lesson"
    origin_log = revision_log_id(draft_id, "origin")
    initial = append(store, record(origin_log, None, (), {
        "type": "draft.revised", "draftId": draft_id, "replicaId": "origin",
        "source": "print(0)", "base": None,
    }))
    requests: list[str] = []
    for replica_id in ("A", "B"):
        log_id = request_log_id(draft_id, replica_id)
        requests.append(log_id)
        append(store, record(log_id, None, (initial,), {
            "type": "draft.beginEditing", "commandId": f"begin-{replica_id}",
            "draftId": draft_id, "replicaId": replica_id,
            "expectedHead": initial.to_value(), "expectedEpoch": 0,
        }))

    outcomes = DraftRequestConsumer(store, tuple(requests)).run_once()
    assert [result.kind for _, result in outcomes] == ["accepted", "activeElsewhere"]
    decisions = store.read(decision_log_id(draft_id), None, 10).records
    assert [decision.data["type"] for decision in decisions] == [
        "draft.started", "draft.editingStarted", "draft.rejected",
    ]
    assert decisions[-1].data["reason"] == "activeElsewhere"

    head = store.head(decision_log_id(draft_id))
    replayed = DraftRequestConsumer(store, tuple(requests)).run_once()
    assert [result.kind for _, result in replayed] == ["current", "activeElsewhere"]
    assert store.head(decision_log_id(draft_id)) == head


def replica_grants_are_fenced() -> None:
    PrincipalSession.create("user", "A", "token", {
        "draft/lesson/revision/A": "append",
        "draft/lesson/request/A": "append",
        "submission/A/requests": "append",
        "submission/A/results": "read",
    })
    try:
        PrincipalSession.create("user", "A", "token", {"draft/lesson/request/B": "append"})
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
    revision = append(store, record("draft/lesson/revision/A", None, (), {
        "type": "draft.revised", "draftId": "lesson", "replicaId": "A",
        "source": "print('branch')", "base": None,
    }))
    request_log = "submission/A/requests"
    request = append(store, record(request_log, None, (revision,), {
        "type": "submission.requested", "submissionId": "one",
        "playgroundId": "lesson", "revision": revision.to_value(),
    }))
    tags = Tags()
    worker = LogWorker(
        store,
        (request_log,),
        submission_result_log(request_log),
        SubmissionHandler(store, tags),
    )
    assert worker.run_once() == 1
    assert worker.run_once() == 0
    assert tags.calls == 1
    result = store.read("submission/A/results", None, 10).records[0]
    assert result.data["type"] == "submission.completed"
    assert result.data["request"] == request.to_value()
    assert result.requires == (request, revision)


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        draft_rejections_are_durable(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        branch_revisions_can_be_submitted(Path(raw))
    replica_grants_are_fenced()
    assert submission_result_log("submission/A/requests") == "submission/A/results"
    print("durable Draft rejection, replica fencing and Submission result routing passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
