from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
import json
from threading import Lock
from time import monotonic
from typing import Protocol

from apps.server.learning_analysis.logs import (
    attempt_log_id,
    evidence_scope,
    is_evidence_log,
    job_log_id,
    result_log_id,
)
from apps.server.learning_analysis.materializers import (
    DEFAULT_MATERIALIZERS,
    LearningEvidenceMaterializer,
    materializer_map,
    resolve_materializer,
)
from apps.server.learning_analysis.protocol import (
    LearningAnalysisInvalidInput,
    decode_assessment,
    decode_evidence_data,
)
from apps.server.log_reads import find_record, read_all
from apps.server.validation import require_position, require_text
from picopg.api import Position, ReplicaLogStore, ReplicatedRecord, record_id_of


ANALYSIS = {"key": "learning.feedback", "revision": "1"}
POLICY = {"key": "immediate-evidence", "revision": "1"}
MAX_EVIDENCE_BYTES = 64 * 1024
RETRY_BASE_SECONDS = 1.0
RETRY_MAX_SECONDS = 300.0


class LearningAnalyzer(Protocol):
    def analyze(
        self,
        *,
        job: Mapping[str, object],
        records: tuple[ReplicatedRecord, ...],
    ) -> Mapping[str, object]: ...


class LearningAnalysisConsumer:
    def __init__(
        self,
        store: ReplicaLogStore,
        evidence_logs: tuple[str, ...],
        analyzer: LearningAnalyzer,
        on_result: Callable[[str, Position], None] | None = None,
        now: Callable[[], float] = monotonic,
        materializers: tuple[LearningEvidenceMaterializer, ...] = DEFAULT_MATERIALIZERS,
    ) -> None:
        if len(set(evidence_logs)) != len(evidence_logs):
            raise ValueError("evidence_logs must be unique")
        if any(not is_evidence_log(log_id) for log_id in evidence_logs):
            raise ValueError("evidence_logs must contain only Evidence Logs")
        self._store = store
        self._evidence_logs = set(evidence_logs)
        self._analyzer = analyzer
        self._on_result = on_result or (lambda _log_id, _head: None)
        self._now = now
        self._materializers = materializer_map(materializers)
        self._lock = Lock()

    def add_evidence_log(self, log_id: str) -> None:
        if not is_evidence_log(log_id):
            raise ValueError("log_id is not an Evidence Log")
        with self._lock:
            self._evidence_logs.add(log_id)

    def run_once(self) -> int:
        with self._lock:
            evidence_logs = tuple(sorted(self._evidence_logs))
        principals = sorted({evidence_scope(log_id)[0] for log_id in evidence_logs})
        created = sum(self._schedule(log_id) for log_id in evidence_logs)
        published = sum(self._publish(principal_id) for principal_id in principals)
        return created + published

    def next_wake_delay(self) -> float | None:
        with self._lock:
            principals = {evidence_scope(log_id)[0] for log_id in self._evidence_logs}
        now = self._now()
        delays: list[float] = []
        for principal_id in principals:
            completed = _completed_job_keys(read_all(self._store, result_log_id(principal_id)))
            attempts = _latest_attempts(read_all(self._store, attempt_log_id(principal_id)))
            for job in read_all(self._store, job_log_id(principal_id)):
                if not _is_job(job.data):
                    continue
                job_key = str(job.data["jobKey"])
                if job_key in completed or job_key not in attempts:
                    continue
                delays.append(max(0.0, attempts[job_key][1] - now))
        return min(delays) if delays else None

    def _schedule(self, evidence_log: str) -> int:
        principal_id, _ = evidence_scope(evidence_log)
        output_log = job_log_id(principal_id)
        jobs = read_all(self._store, output_log)
        handled = {
            (position.log_id, position.record_id)
            for record in jobs
            if (position := _decision_evidence(record)) is not None
        }
        known_sources = {
            _source_key(record.data): evidence
            for record in jobs
            if _is_job(record.data)
            if (evidence := _decision_evidence(record)) is not None
        }
        head = None if not jobs else jobs[-1].position
        created = 0
        for evidence in read_all(self._store, evidence_log):
            position_key = evidence.log_id, evidence.id
            if position_key in handled:
                continue
            try:
                decoded = _decode_evidence(evidence)
                materializer = resolve_materializer(decoded, self._materializers)
            except LearningAnalysisInvalidInput as error:
                data = {
                    "version": 1,
                    "type": "learning.analysis.evidence.rejected",
                    "evidence": evidence.position.to_value(),
                    "reason": str(error) or "Evidence is invalid",
                }
                record = _record(output_log, head, (evidence.position,), data)
                appended = self._store.append(output_log, head, (record,))
                if appended.kind != "accepted":
                    raise RuntimeError(f"{output_log} lost its single writer") from None
                head = record.position
                handled.add(position_key)
                created += 1
                continue
            source_key = _source_key(decoded)
            if source_key in known_sources:
                original = find_record(self._store, known_sources[source_key])
                if original is None:
                    raise RuntimeError("Original Evidence is missing")
                data = {
                    "version": 1,
                    "type": (
                        "learning.analysis.evidence.duplicate"
                        if original.data == evidence.data
                        else "learning.analysis.evidence.source-conflict"
                    ),
                    "evidence": evidence.position.to_value(),
                    "source": decoded["source"],
                }
                record = _record(output_log, head, (evidence.position,), data)
                appended = self._store.append(output_log, head, (record,))
                if appended.kind != "accepted":
                    raise RuntimeError(f"{output_log} lost its single writer")
                head = record.position
                handled.add(position_key)
                created += 1
                continue
            try:
                data, dependencies = _job_data(
                    self._store, evidence, decoded, principal_id, materializer
                )
            except LearningAnalysisInvalidInput as error:
                data = {
                    "version": 1,
                    "type": "learning.analysis.evidence.rejected",
                    "evidence": evidence.position.to_value(),
                    "reason": str(error) or "Evidence inputs are invalid",
                }
                record = _record(output_log, head, (evidence.position,), data)
                appended = self._store.append(output_log, head, (record,))
                if appended.kind != "accepted":
                    raise RuntimeError(f"{output_log} lost its single writer") from None
                head = record.position
                handled.add(position_key)
                created += 1
                continue
            record = _record(output_log, head, dependencies, data)
            appended = self._store.append(output_log, head, (record,))
            if appended.kind != "accepted":
                raise RuntimeError(f"{output_log} lost its single writer")
            head = record.position
            handled.add(position_key)
            known_sources[source_key] = evidence.position
            created += 1
        return created

    def _publish(self, principal_id: str) -> int:
        jobs = read_all(self._store, job_log_id(principal_id))
        output_log = result_log_id(principal_id)
        results = read_all(self._store, output_log)
        completed = _completed_job_keys(results)
        attempts = _latest_attempts(read_all(self._store, attempt_log_id(principal_id)))
        published = 0
        for job_record in jobs:
            if not _is_job(job_record.data):
                continue
            job = _decode_job(job_record.data)
            job_key = require_text(job["jobKey"], "jobKey")
            if job_key in completed:
                continue
            retry = attempts.get(job_key)
            if retry is not None and retry[1] > self._now():
                continue
            records = tuple(
                record
                for position in job_record.requires
                if (record := find_record(self._store, position)) is not None
            )
            if len(records) != len(job_record.requires):
                raise RuntimeError("Job input is missing")
            try:
                output = decode_assessment(self._analyzer.analyze(job=job, records=records))
                data: Mapping[str, object] = {
                    "version": 1,
                    "type": "learning.analysis.completed",
                    "jobKey": job_key,
                    "subject": job["subject"],
                    "analysis": job["analysis"],
                    "basis": job["basis"],
                    "output": output,
                }
            except LearningAnalysisInvalidInput as error:
                data = {
                    "version": 1,
                    "type": "learning.analysis.failed",
                    "jobKey": job_key,
                    "subject": job["subject"],
                    "analysis": job["analysis"],
                    "basis": job["basis"],
                    "error": {
                        "code": "invalid-input",
                        "message": str(error) or "Analysis input is invalid",
                    },
                }
            except Exception:
                attempts[job_key] = self._record_retry(principal_id, job_key)
                continue
            if self._append_terminal(output_log, job_key, data):
                completed.add(job_key)
                published += 1
        return published

    def _append_terminal(self, output_log: str, job_key: str, data: Mapping[str, object]) -> bool:
        while True:
            results = read_all(self._store, output_log)
            if job_key in _completed_job_keys(results):
                return False
            head = None if not results else results[-1].position
            record = _record(output_log, head, (), data)
            appended = self._store.append(output_log, head, (record,))
            if appended.kind == "accepted":
                self._on_result(output_log, record.position)
                return True
            if appended.kind not in {"changed", "current"}:
                raise RuntimeError(f"Unexpected append outcome {appended.kind}")

    def _record_retry(self, principal_id: str, job_key: str) -> tuple[int, float]:
        output_log = attempt_log_id(principal_id)
        while True:
            records = read_all(self._store, output_log)
            attempts = _latest_attempts(records)
            attempt = attempts.get(job_key, (0, 0.0))[0] + 1
            retry_at = self._now() + min(
                RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            )
            data = {
                "version": 1,
                "type": "learning.analysis.attempt.failed",
                "jobKey": job_key,
                "attempt": attempt,
                "retryAt": retry_at,
            }
            head = None if not records else records[-1].position
            record = _record(output_log, head, (), data)
            appended = self._store.append(output_log, head, (record,))
            if appended.kind == "accepted":
                return attempt, retry_at
            if appended.kind not in {"changed", "current"}:
                raise RuntimeError(f"Unexpected append outcome {appended.kind}")


def _decode_evidence(record: ReplicatedRecord) -> Mapping[str, object]:
    data = record.data
    encoded = json.dumps(
        _json(data), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise LearningAnalysisInvalidInput(f"Evidence exceeds {MAX_EVIDENCE_BYTES} bytes")
    return decode_evidence_data(data)


def _source_key(value: Mapping[str, object]) -> tuple[str, int]:
    source = value["source"]
    if not isinstance(source, Mapping):
        raise LearningAnalysisInvalidInput("source is invalid")
    return str(source["session"]), int(source["sequence"])


def _job_data(
    store: ReplicaLogStore,
    evidence: ReplicatedRecord,
    decoded: Mapping[str, object],
    principal_id: str,
    materializer: LearningEvidenceMaterializer,
) -> tuple[Mapping[str, object], tuple[Position, ...]]:
    subject = decoded["subject"]
    basis = _canonical_basis(store, ((evidence.parent, evidence.position),))
    context = {
        "schema": "learning.course-context",
        "version": 1,
        "data": subject,
    }
    materialized = materializer.materialize(
        store=store,
        evidence=evidence,
        decoded=decoded,
        principal_id=principal_id,
    )
    inputs: list[Mapping[str, object]] = [
        {"role": "evidence", "record": evidence.position.to_value()},
        *materialized.inputs,
    ]
    dependencies = [evidence.position, *materialized.dependencies]
    frozen = {
        "subject": subject,
        "analysis": ANALYSIS,
        "policy": POLICY,
        "basis": basis,
        "inputs": [*inputs, {"role": "course-context", "snapshot": context}],
    }
    job_key = "sha256:" + sha256(_canonical(frozen)).hexdigest()
    return (
        {
            "version": 2,
            "type": "learning.analysis.job.created",
            "jobKey": job_key,
            "evidence": evidence.position.to_value(),
            "source": decoded["source"],
            **frozen,
        },
        tuple(dependencies),
    )


def _decode_job(value: object) -> Mapping[str, object]:
    if not _is_job(value):
        raise LearningAnalysisInvalidInput("Job shape is invalid")
    return value


def _is_job(value: object) -> bool:
    if not isinstance(value, Mapping) or value.get("type") != "learning.analysis.job.created":
        return False
    if value.get("version") == 1:
        return set(value) == {
            "version",
            "type",
            "jobKey",
            "source",
            "subject",
            "analysis",
            "basis",
            "inputs",
        }
    return value.get("version") == 2 and set(value) == {
        "version",
        "type",
        "jobKey",
        "evidence",
        "source",
        "subject",
        "analysis",
        "policy",
        "basis",
        "inputs",
    }


def _decision_evidence(record: ReplicatedRecord) -> Position | None:
    data = record.data
    if not isinstance(data, Mapping):
        return None
    explicit = data.get("evidence")
    if explicit is not None:
        try:
            return require_position(explicit, "evidence", error_type=LearningAnalysisInvalidInput)
        except LearningAnalysisInvalidInput:
            return None
    if _is_job(data):
        inputs = data.get("inputs")
        if isinstance(inputs, tuple):
            for value in inputs:
                if isinstance(value, Mapping) and value.get("role") == "evidence":
                    try:
                        return require_position(
                            value.get("record"),
                            "inputs.evidence.record",
                            error_type=LearningAnalysisInvalidInput,
                        )
                    except LearningAnalysisInvalidInput:
                        return None
    # Compatibility for v1 scheduler decisions written before evidence became explicit.
    return record.requires[0] if data.get("version") == 1 and record.requires else None


def _canonical_basis(
    store: ReplicaLogStore,
    windows: tuple[tuple[Position | None, Position], ...],
) -> Mapping[str, object]:
    resolved: list[tuple[bytes, int, int, Position | None, Position]] = []
    for after, through in windows:
        if after is not None and after.log_id != through.log_id:
            raise LearningAnalysisInvalidInput("Evidence window spans different Logs")
        records = read_all(store, through.log_id)
        indexes = {record.id: index for index, record in enumerate(records)}
        if through.record_id not in indexes:
            raise LearningAnalysisInvalidInput("Evidence window through is unknown")
        start = -1 if after is None else indexes.get(after.record_id)
        if start is None:
            raise LearningAnalysisInvalidInput("Evidence window after is unknown")
        end = indexes[through.record_id]
        if start >= end:
            raise LearningAnalysisInvalidInput("Evidence window is reversed")
        resolved.append((through.log_id.encode("utf-8"), start, end, after, through))
    resolved.sort(key=lambda item: (item[0], item[1], item[2]))
    previous: tuple[bytes, int] | None = None
    for log_id, start, end, _, _ in resolved:
        if previous is not None and previous[0] == log_id and start < previous[1]:
            raise LearningAnalysisInvalidInput("Evidence windows overlap")
        previous = log_id, end
    return {
        "evidence": [
            {
                "after": None if after is None else after.to_value(),
                "through": through.to_value(),
            }
            for _, _, _, after, through in resolved
        ]
    }


def _completed_job_keys(records: tuple[ReplicatedRecord, ...]) -> set[str]:
    return {
        job_key
        for record in records
        if isinstance(record.data, Mapping)
        if isinstance(job_key := record.data.get("jobKey"), str)
    }


def _latest_attempts(records: tuple[ReplicatedRecord, ...]) -> dict[str, tuple[int, float]]:
    attempts: dict[str, tuple[int, float]] = {}
    for record in records:
        data = record.data
        if (
            not isinstance(data, Mapping)
            or data.get("version") != 1
            or data.get("type") != "learning.analysis.attempt.failed"
            or not isinstance(data.get("jobKey"), str)
            or isinstance(data.get("attempt"), bool)
            or not isinstance(data.get("attempt"), int)
            or isinstance(data.get("retryAt"), bool)
            or not isinstance(data.get("retryAt"), (int, float))
        ):
            continue
        attempts[data["jobKey"]] = (data["attempt"], float(data["retryAt"]))
    return attempts


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _record(
    log_id: str,
    parent: Position | None,
    requires: tuple[Position, ...],
    data: Mapping[str, object],
) -> ReplicatedRecord:
    normalized = {key: _json(item) for key, item in data.items()}
    return ReplicatedRecord(
        record_id_of(log_id, parent, requires, normalized),
        log_id,
        parent,
        requires,
        normalized,
    )


def _json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_json(item) for item in value)
    return value
