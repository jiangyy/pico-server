from __future__ import annotations

import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for entry in (ROOT, SRC):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from apps.server.learning_analysis.logs import (  # noqa: E402
    evidence_log_id,
    job_log_id,
    result_log_id,
)
from apps.server.learning_analysis.worker import LearningAnalysisConsumer  # noqa: E402
from apps.server.log_reads import read_all  # noqa: E402
from picopg.api import Position, ReplicatedRecord, record_id_of  # noqa: E402
from picopg.impl.file_replica_log import FileReplicaLogStore  # noqa: E402


def record(
    log_id: str,
    parent: Position | None,
    requires: tuple[Position, ...],
    data: dict[str, object],
) -> ReplicatedRecord:
    return ReplicatedRecord(
        record_id_of(log_id, parent, requires, data), log_id, parent, requires, data
    )


def append(store: FileReplicaLogStore, value: ReplicatedRecord) -> Position:
    result = store.append(value.log_id, value.parent, (value,))
    assert result.kind == "accepted"
    return value.position


def assessment(summary: str) -> dict[str, object]:
    return {
        "schema": "learning.assessment",
        "version": 1,
        "data": {
            "summary": summary,
            "findings": [],
            "recommendations": [],
            "limitations": ["deterministic test analyzer"],
        },
    }


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain(item) for item in value]
    return value


def submission_evidence(
    log_id: str,
    parent: Position | None,
    request: Position,
    *,
    session: str,
    sequence: int,
    version_id: str = "sha256:course",
) -> ReplicatedRecord:
    return record(
        log_id,
        parent,
        (request,),
        {
            "version": 1,
            "type": "learning.evidence.recorded",
            "source": {"session": session, "sequence": sequence},
            "subject": {"playgroundId": "loops", "versionId": version_id},
            "payload": {
                "schema": "learning.submission.requested",
                "version": 1,
                "data": {"request": request.to_value()},
            },
        },
    )


class Analyzer:
    def __init__(self, summary: str = "分析完成") -> None:
        self.summary = summary
        self.calls = 0

    def analyze(self, *, job, records):
        self.calls += 1
        assert job["inputs"][0]["record"] == records[0].position.to_value()
        return assessment(self.summary)


def submission_request(store: FileReplicaLogStore) -> tuple[Position, Position]:
    revision = append(
        store,
        record(
            "principal/user/draft/loops/revision/A",
            None,
            (),
            {
                "type": "draft.revised",
                "draftId": "loops",
                "replicaId": "A",
                "source": "for item in range(3):\n    print(item)\n",
                "base": None,
            },
        ),
    )
    request = append(
        store,
        record(
            "principal/user/submission/A/requests",
            None,
            (revision,),
            {
                "type": "submission.requested",
                "submissionId": "one",
                "playgroundId": "loops",
                "revision": revision.to_value(),
            },
        ),
    )
    return request, revision


def durable_job_and_result_are_idempotent(root: Path) -> None:
    store = FileReplicaLogStore(root)
    request, revision = submission_request(store)
    evidence_log = evidence_log_id("user", "A")
    evidence = append(
        store, submission_evidence(evidence_log, None, request, session="capture-a", sequence=1)
    )
    analyzer = Analyzer()
    notified: list[tuple[str, Position]] = []
    consumer = LearningAnalysisConsumer(
        store, (evidence_log,), analyzer, lambda log_id, head: notified.append((log_id, head))
    )

    assert consumer.run_once() == 2
    assert consumer.run_once() == 0
    assert analyzer.calls == 1

    jobs = read_all(store, job_log_id("user"))
    assert len(jobs) == 1 and jobs[0].requires == (evidence, request, revision)
    job = jobs[0].data
    assert plain(job["basis"]) == {"evidence": [{"after": None, "through": evidence.to_value()}]}
    assert [item["role"] for item in job["inputs"]] == [
        "evidence",
        "submission-request",
        "draft-revision",
        "course-context",
    ]
    assert plain(job["inputs"][3]) == {
        "role": "course-context",
        "snapshot": {
            "schema": "learning.course-context",
            "version": 1,
            "data": {"playgroundId": "loops", "versionId": "sha256:course"},
        },
    }

    results = read_all(store, result_log_id("user"))
    assert len(results) == 1 and results[0].requires == ()
    assert results[0].data["type"] == "learning.analysis.completed"
    assert results[0].data["jobKey"] == job["jobKey"]
    assert plain(results[0].data["output"]) == assessment("分析完成")
    assert notified == [(result_log_id("user"), results[0].position)]

    restarted = LearningAnalysisConsumer(store, (evidence_log,), Analyzer("different"))
    assert restarted.run_once() == 0


def duplicate_and_invalid_evidence_do_not_block(root: Path) -> None:
    store = FileReplicaLogStore(root)
    request, revision = submission_request(store)
    evidence_log = evidence_log_id("user", "A")
    invalid = append(store, record(evidence_log, None, (), {"type": "broken"}))
    first = append(
        store, submission_evidence(evidence_log, invalid, request, session="capture-a", sequence=1)
    )
    duplicate = append(
        store, submission_evidence(evidence_log, first, request, session="capture-a", sequence=1)
    )
    conflict = append(
        store,
        submission_evidence(
            evidence_log,
            duplicate,
            request,
            session="capture-a",
            sequence=1,
            version_id="sha256:different",
        ),
    )
    analyzer = Analyzer()
    consumer = LearningAnalysisConsumer(store, (evidence_log,), analyzer)

    assert consumer.run_once() == 5
    assert consumer.run_once() == 0
    assert analyzer.calls == 1
    jobs = read_all(store, job_log_id("user"))
    assert [item.data["type"] for item in jobs] == [
        "learning.analysis.evidence.rejected",
        "learning.analysis.job.created",
        "learning.analysis.evidence.duplicate",
        "learning.analysis.evidence.source-conflict",
    ]
    assert [item.requires for item in jobs] == [
        (invalid,),
        (first, request, revision),
        (duplicate,),
        (conflict,),
    ]
    assert len(read_all(store, result_log_id("user"))) == 1


def transient_failure_reuses_the_frozen_job(root: Path) -> None:
    class FlakyAnalyzer(Analyzer):
        def analyze(self, *, job, records):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider unavailable")
            return assessment(job["jobKey"])

    store = FileReplicaLogStore(root)
    request, _ = submission_request(store)
    evidence_log = evidence_log_id("user", "A")
    append(store, submission_evidence(evidence_log, None, request, session="capture-a", sequence=1))
    analyzer = FlakyAnalyzer()
    consumer = LearningAnalysisConsumer(store, (evidence_log,), analyzer)
    try:
        consumer.run_once()
    except RuntimeError as error:
        assert str(error) == "provider unavailable"
    else:
        raise AssertionError("transient analyzer failure was published as terminal")
    frozen_job = read_all(store, job_log_id("user"))[0]
    assert read_all(store, result_log_id("user")) == ()

    assert consumer.run_once() == 1
    result = read_all(store, result_log_id("user"))[0]
    assert result.data["jobKey"] == frozen_job.data["jobKey"]
    assert result.data["output"]["data"]["summary"] == frozen_job.data["jobKey"]


def invalid_output_is_a_terminal_failure(root: Path) -> None:
    class InvalidAnalyzer:
        def analyze(self, *, job, records):
            return {"schema": "learning.assessment", "version": 1, "data": {}}

    store = FileReplicaLogStore(root)
    request, _ = submission_request(store)
    evidence_log = evidence_log_id("user", "A")
    append(store, submission_evidence(evidence_log, None, request, session="capture-a", sequence=1))
    consumer = LearningAnalysisConsumer(store, (evidence_log,), InvalidAnalyzer())
    assert consumer.run_once() == 2
    result = read_all(store, result_log_id("user"))[0]
    assert result.data["type"] == "learning.analysis.failed"
    assert result.data["error"]["code"] == "invalid-input"
    assert consumer.run_once() == 0


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        durable_job_and_result_are_idempotent(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        duplicate_and_invalid_evidence_do_not_block(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        transient_failure_reuses_the_frozen_job(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        invalid_output_is_a_terminal_failure(Path(raw))
    print("learning analysis Jobs, Results, deduplication and retry semantics passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
