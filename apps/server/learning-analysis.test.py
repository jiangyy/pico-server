from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for entry in (ROOT, SRC):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from apps.server.learning_analysis.logs import (  # noqa: E402
    attempt_log_id,
    evidence_log_id,
    job_log_id,
    result_log_id,
)
from apps.server.learning_analysis.pipeline import (  # noqa: E402
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    LearningFeedbackAnalyzer,
)
from apps.server.application import PicoPGHttpServer  # noqa: E402
from apps.server.draft.protocol import (  # noqa: E402
    decision_log_id,
    request_log_id,
    revision_log_id,
)
from apps.server.learning_analysis.worker import (  # noqa: E402
    MAX_EVIDENCE_BYTES,
    LearningAnalysisConsumer,
    LearningAnalysisInvalidInput,
    _canonical_basis,
)
from apps.server.log_reads import read_all  # noqa: E402
from apps.server.worker_thread import ServerWorkerThread  # noqa: E402
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
        evidence = next(value for value in job["inputs"] if value["role"] == "evidence")
        assert evidence["record"] in [item.position.to_value() for item in records]
        return assessment(self.summary)


class Provider:
    def __init__(self) -> None:
        self.call: dict[str, object] | None = None

    def generate(self, *, analysis, system, input, output_schema):
        self.call = {
            "analysis": analysis,
            "system": system,
            "input": input,
            "output_schema": output_schema,
        }
        return assessment("你已经能够正确使用 range 完成遍历。")["data"]


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
    assert job["version"] == 2
    assert plain(job["policy"]) == {"key": "immediate-evidence", "revision": "1"}
    assert job["evidence"] == evidence.to_value()
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
    now = [100.0]
    consumer = LearningAnalysisConsumer(store, (evidence_log,), analyzer, now=lambda: now[0])
    assert consumer.run_once() == 1
    frozen_job = read_all(store, job_log_id("user"))[0]
    assert read_all(store, result_log_id("user")) == ()
    attempts = read_all(store, attempt_log_id("user"))
    assert len(attempts) == 1
    assert attempts[0].data["attempt"] == 1
    assert attempts[0].data["retryAt"] == 101.0
    assert consumer.next_wake_delay() == 1.0
    assert consumer.run_once() == 0
    assert analyzer.calls == 1

    now[0] = 101.0
    assert consumer.run_once() == 1
    result = read_all(store, result_log_id("user"))[0]
    assert result.data["jobKey"] == frozen_job.data["jobKey"]
    assert result.data["output"]["data"]["summary"] == frozen_job.data["jobKey"]
    assert analyzer.calls == 2


def unsupported_and_oversized_evidence_are_rejected_before_jobs(root: Path) -> None:
    store = FileReplicaLogStore(root)
    request, _ = submission_request(store)
    evidence_log = evidence_log_id("user", "A")
    unsupported = record(
        evidence_log,
        None,
        (),
        {
            "version": 1,
            "type": "learning.evidence.recorded",
            "source": {"session": "capture-a", "sequence": 1},
            "subject": {"playgroundId": "loops", "versionId": "sha256:course"},
            "payload": {"schema": "learning.future", "version": 1, "data": {}},
        },
    )
    append(store, unsupported)
    oversized = submission_evidence(
        evidence_log, unsupported.position, request, session="x" * MAX_EVIDENCE_BYTES, sequence=2
    )
    append(store, oversized)
    analyzer = Analyzer()
    consumer = LearningAnalysisConsumer(store, (evidence_log,), analyzer)

    assert consumer.run_once() == 2
    decisions = read_all(store, job_log_id("user"))
    assert [item.data["type"] for item in decisions] == [
        "learning.analysis.evidence.rejected",
        "learning.analysis.evidence.rejected",
    ]
    assert decisions[0].data["reason"] == "Evidence payload schema is unsupported"
    assert decisions[1].data["reason"] == f"Evidence exceeds {MAX_EVIDENCE_BYTES} bytes"
    assert analyzer.calls == 0
    assert read_all(store, result_log_id("user")) == ()


def basis_is_ancestral_non_overlapping_and_canonical(root: Path) -> None:
    store = FileReplicaLogStore(root)
    log_a = evidence_log_id("user", "A")
    log_b = evidence_log_id("user", "B")
    a1 = record(log_a, None, (), {"value": "a1"})
    append(store, a1)
    a2 = record(log_a, a1.position, (), {"value": "a2"})
    append(store, a2)
    a3 = record(log_a, a2.position, (), {"value": "a3"})
    append(store, a3)
    b1 = record(log_b, None, (), {"value": "b1"})
    append(store, b1)

    basis = _canonical_basis(
        store,
        ((None, b1.position), (a1.position, a3.position), (None, a1.position)),
    )
    assert plain(basis) == {
        "evidence": [
            {"after": None, "through": a1.position.to_value()},
            {"after": a1.position.to_value(), "through": a3.position.to_value()},
            {"after": None, "through": b1.position.to_value()},
        ]
    }
    try:
        _canonical_basis(store, ((a2.position, a1.position),))
    except LearningAnalysisInvalidInput as error:
        assert str(error) == "Evidence window is reversed"
    else:
        raise AssertionError("reversed Evidence window was accepted")
    try:
        _canonical_basis(store, ((None, a2.position), (a1.position, a3.position)))
    except LearningAnalysisInvalidInput as error:
        assert str(error) == "Evidence windows overlap"
    else:
        raise AssertionError("overlapping Evidence windows were accepted")


def historical_jobs_and_reordered_dependencies_remain_processable(root: Path) -> None:
    store = FileReplicaLogStore(root)
    request, revision = submission_request(store)
    evidence_log = evidence_log_id("user", "A")
    evidence = append(
        store, submission_evidence(evidence_log, None, request, session="capture-a", sequence=1)
    )
    scheduler = LearningAnalysisConsumer(store, (evidence_log,), Analyzer())
    assert scheduler._schedule(evidence_log) == 1
    created = read_all(store, job_log_id("user"))[0]
    current = created.data
    legacy = {key: value for key, value in current.items() if key not in {"evidence", "policy"}}
    legacy["version"] = 1
    legacy["jobKey"] = "legacy-job"
    output_log = job_log_id("historical")
    append(store, record(output_log, None, (request, revision, evidence), legacy))
    consumer = LearningAnalysisConsumer(store, (), Analyzer("legacy"))
    assert consumer._publish("historical") == 1
    result = read_all(store, result_log_id("historical"))[0]
    assert result.data["jobKey"] == "legacy-job"


def concurrent_publishers_recheck_job_key_after_head_conflict(root: Path) -> None:
    store = FileReplicaLogStore(root)
    request, _ = submission_request(store)
    evidence_log = evidence_log_id("user", "A")
    append(store, submission_evidence(evidence_log, None, request, session="capture-a", sequence=1))
    scheduler = LearningAnalysisConsumer(store, (evidence_log,), Analyzer())
    assert scheduler._schedule(evidence_log) == 1
    barrier = threading.Barrier(2)

    class ConcurrentAnalyzer(Analyzer):
        def analyze(self, *, job, records):
            result = super().analyze(job=job, records=records)
            barrier.wait(timeout=2)
            return result

    notified: list[Position] = []
    consumers = [
        LearningAnalysisConsumer(
            store,
            (evidence_log,),
            ConcurrentAnalyzer(),
            lambda _log_id, head: notified.append(head),
        )
        for _ in range(2)
    ]
    failures: list[BaseException] = []

    def publish(consumer: LearningAnalysisConsumer) -> None:
        try:
            consumer._publish("user")
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=publish, args=(consumer,)) for consumer in consumers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert failures == []
    assert len(read_all(store, result_log_id("user"))) == 1
    assert len(notified) == 1


def retry_backoff_wakes_without_another_append(root: Path) -> None:
    store = FileReplicaLogStore(root)
    request, _ = submission_request(store)
    evidence_log = evidence_log_id("user", "A")
    append(store, submission_evidence(evidence_log, None, request, session="capture-a", sequence=1))

    class OnceUnavailable(Analyzer):
        def analyze(self, *, job, records):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider unavailable")
            return assessment("retried")

    analyzer = OnceUnavailable()
    consumer = LearningAnalysisConsumer(store, (evidence_log,), analyzer)
    worker = ServerWorkerThread((consumer,))
    worker.start()
    deadline = time.monotonic() + 2.5
    try:
        while not read_all(store, result_log_id("user")) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(read_all(store, result_log_id("user"))) == 1
        assert analyzer.calls == 2
    finally:
        worker.close()


def blocked_analysis_does_not_block_draft_worker(root: Path) -> None:
    store = FileReplicaLogStore(root / "replica")
    submission, _ = submission_request(store)
    evidence_log = evidence_log_id("user", "A")
    append(
        store,
        submission_evidence(evidence_log, None, submission, session="capture-a", sequence=1),
    )
    draft_id = "independent"
    draft_revision_log = revision_log_id("user", draft_id, "A")
    revision = append(
        store,
        record(
            draft_revision_log,
            None,
            (),
            {
                "type": "draft.revised",
                "draftId": draft_id,
                "replicaId": "A",
                "source": "print('draft')",
                "base": None,
            },
        ),
    )
    entered = threading.Event()
    release = threading.Event()

    class BlockingAnalyzer(Analyzer):
        def analyze(self, *, job, records):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test did not release analyzer")
            return super().analyze(job=job, records=records)

    server = PicoPGHttpServer(
        ("127.0.0.1", 0),
        store,
        "secret",
        root / "packages",
        learning_analyzer=BlockingAnalyzer(),
    )
    try:
        assert entered.wait(1)
        requests = request_log_id("user", draft_id, "A")
        command = record(
            requests,
            None,
            (revision,),
            {
                "type": "draft.beginEditing",
                "commandId": "begin-independent",
                "draftId": draft_id,
                "replicaId": "A",
                "expectedHead": revision.to_value(),
                "expectedEpoch": 0,
            },
        )
        assert server.append_and_broadcast(requests, None, (command,)).kind == "accepted"
        deadline = time.monotonic() + 1
        decisions = decision_log_id("user", draft_id)
        while store.head(decisions) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert store.head(decisions) is not None
        assert read_all(store, result_log_id("user")) == ()
    finally:
        release.set()
        server.server_close()


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


def feedback_pipeline_exposes_only_the_versioned_prompt_contract(root: Path) -> None:
    store = FileReplicaLogStore(root)
    request, _ = submission_request(store)
    evidence_log = evidence_log_id("user", "A")
    append(store, submission_evidence(evidence_log, None, request, session="capture-a", sequence=1))
    provider = Provider()
    consumer = LearningAnalysisConsumer(
        store,
        (evidence_log,),
        LearningFeedbackAnalyzer(provider),
    )

    assert consumer.run_once() == 2
    assert provider.call is not None
    call = provider.call
    assert call["analysis"] == "learning.feedback@1"
    assert call["system"] == SYSTEM_PROMPT
    assert "不推测身份" in SYSTEM_PROMPT
    assert plain(call["input"]) == {
        "language": "zh-CN",
        "subject": {"playgroundId": "loops", "versionId": "sha256:course"},
        "courseContext": {"playgroundId": "loops", "versionId": "sha256:course"},
        "evidence": [
            {
                "schema": "learning.submission.requested",
                "version": 1,
                "data": {"event": "submission-requested"},
            }
        ],
        "submission": {"source": "for item in range(3):\n    print(item)\n"},
    }
    serialized = json.dumps(plain(call["input"]), ensure_ascii=False, sort_keys=True)
    for private_field in ("principal/", "accessCode", "jobKey", "session", "recordId"):
        assert private_field not in serialized
    schema = plain(call["output_schema"])
    assert schema == plain(OUTPUT_SCHEMA)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["summary", "findings", "recommendations", "limitations"]
    result = read_all(store, result_log_id("user"))[0]
    assert result.data["type"] == "learning.analysis.completed"
    assert result.data["output"]["data"]["summary"] == "你已经能够正确使用 range 完成遍历。"


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        durable_job_and_result_are_idempotent(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        duplicate_and_invalid_evidence_do_not_block(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        transient_failure_reuses_the_frozen_job(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        invalid_output_is_a_terminal_failure(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        unsupported_and_oversized_evidence_are_rejected_before_jobs(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        basis_is_ancestral_non_overlapping_and_canonical(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        historical_jobs_and_reordered_dependencies_remain_processable(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        concurrent_publishers_recheck_job_key_after_head_conflict(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        retry_backoff_wakes_without_another_append(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        blocked_analysis_does_not_block_draft_worker(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        feedback_pipeline_exposes_only_the_versioned_prompt_contract(Path(raw))
    print("learning analysis Jobs, Results, retries and prompt contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
