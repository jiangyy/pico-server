from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
import json
from threading import Lock
from typing import Protocol

from apps.server.learning_analysis.logs import (
    evidence_scope,
    is_evidence_log,
    job_log_id,
    result_log_id,
)
from apps.server.log_reads import find_record, read_all
from apps.server.submission.logs import request_scope
from apps.server.validation import require_position, require_text
from picopg.api import Position, ReplicaLogStore, ReplicatedRecord, record_id_of


ANALYSIS = {"key": "learning.feedback", "revision": "1"}


class LearningAnalysisInvalidInput(ValueError):
    pass


class LearningAnalyzer(Protocol):
    def analyze(
        self,
        *,
        job: Mapping[str, object],
        evidence: tuple[ReplicatedRecord, ...],
    ) -> Mapping[str, object]: ...


class LearningAnalysisConsumer:
    def __init__(
        self,
        store: ReplicaLogStore,
        evidence_logs: tuple[str, ...],
        analyzer: LearningAnalyzer,
        on_result: Callable[[str, Position], None] | None = None,
    ) -> None:
        if len(set(evidence_logs)) != len(evidence_logs):
            raise ValueError("evidence_logs must be unique")
        if any(not is_evidence_log(log_id) for log_id in evidence_logs):
            raise ValueError("evidence_logs must contain only Evidence Logs")
        self._store = store
        self._evidence_logs = set(evidence_logs)
        self._analyzer = analyzer
        self._on_result = on_result or (lambda _log_id, _head: None)
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

    def _schedule(self, evidence_log: str) -> int:
        principal_id, _ = evidence_scope(evidence_log)
        output_log = job_log_id(principal_id)
        jobs = read_all(self._store, output_log)
        handled = {
            (position.log_id, position.record_id)
            for record in jobs
            for position in record.requires[:1]
        }
        known_sources = {
            _source_key(record.data): record.requires[0]
            for record in jobs
            if _is_job(record.data) and record.requires
        }
        head = None if not jobs else jobs[-1].position
        created = 0
        for evidence in read_all(self._store, evidence_log):
            position_key = evidence.log_id, evidence.id
            if position_key in handled:
                continue
            try:
                decoded = _decode_evidence(evidence, principal_id)
            except LearningAnalysisInvalidInput as error:
                data = {
                    "version": 1,
                    "type": "learning.analysis.evidence.rejected",
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
            data = _job_data(evidence, decoded)
            record = _record(output_log, head, (evidence.position,), data)
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
        completed = {
            record.data.get("jobKey")
            for record in results
            if isinstance(record.data, Mapping) and isinstance(record.data.get("jobKey"), str)
        }
        head = None if not results else results[-1].position
        published = 0
        for job_record in jobs:
            if not _is_job(job_record.data):
                continue
            job = _decode_job(job_record.data)
            job_key = require_text(job["jobKey"], "jobKey")
            if job_key in completed:
                continue
            evidence = tuple(
                record
                for position in job_record.requires
                if (record := find_record(self._store, position)) is not None
            )
            if len(evidence) != len(job_record.requires):
                raise RuntimeError("Job Evidence is missing")
            try:
                output = _decode_assessment(self._analyzer.analyze(job=job, evidence=evidence))
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
            record = _record(output_log, head, (), data)
            appended = self._store.append(output_log, head, (record,))
            if appended.kind != "accepted":
                raise RuntimeError(f"{output_log} lost its single writer")
            head = record.position
            completed.add(job_key)
            self._on_result(output_log, head)
            published += 1
        return published


def _decode_evidence(record: ReplicatedRecord, principal_id: str) -> Mapping[str, object]:
    data = record.data
    if not isinstance(data, Mapping) or set(data) != {
        "version",
        "type",
        "source",
        "subject",
        "payload",
    }:
        raise LearningAnalysisInvalidInput("Evidence shape is invalid")
    if data["version"] != 1 or data["type"] != "learning.evidence.recorded":
        raise LearningAnalysisInvalidInput("Evidence version or type is invalid")
    source = data["source"]
    if not isinstance(source, Mapping) or set(source) != {"session", "sequence"}:
        raise LearningAnalysisInvalidInput("Evidence source is invalid")
    require_text(source.get("session"), "source.session", error_type=LearningAnalysisInvalidInput)
    sequence = source.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise LearningAnalysisInvalidInput("source.sequence must be a positive integer")
    subject = _decode_subject(data["subject"])
    payload = data["payload"]
    if not isinstance(payload, Mapping) or set(payload) != {"schema", "version", "data"}:
        raise LearningAnalysisInvalidInput("Evidence payload is invalid")
    require_text(payload.get("schema"), "payload.schema", error_type=LearningAnalysisInvalidInput)
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise LearningAnalysisInvalidInput("payload.version must be a positive integer")
    if payload["schema"] == "learning.submission.requested":
        detail = payload["data"]
        if not isinstance(detail, Mapping) or set(detail) != {"request"}:
            raise LearningAnalysisInvalidInput("Submission Evidence data is invalid")
        request = require_position(
            detail["request"], "payload.data.request", error_type=LearningAnalysisInvalidInput
        )
        if request not in record.requires:
            raise LearningAnalysisInvalidInput("Submission Evidence does not require its request")
        try:
            request_principal, _ = request_scope(request.log_id)
        except ValueError as error:
            raise LearningAnalysisInvalidInput("Submission request Log is invalid") from error
        if request_principal != principal_id:
            raise LearningAnalysisInvalidInput("Submission belongs to another Principal")
    return {
        "version": 1,
        "type": "learning.evidence.recorded",
        "source": {"session": source["session"], "sequence": sequence},
        "subject": subject,
        "payload": payload,
    }


def _decode_subject(value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise LearningAnalysisInvalidInput("subject is invalid")
    expected = {"playgroundId", "versionId"} | (
        {"exerciseKey"} if "exerciseKey" in value else set()
    )
    if set(value) != expected:
        raise LearningAnalysisInvalidInput("subject shape is invalid")
    subject = {
        "playgroundId": require_text(
            value.get("playgroundId"),
            "subject.playgroundId",
            error_type=LearningAnalysisInvalidInput,
        ),
        "versionId": require_text(
            value.get("versionId"), "subject.versionId", error_type=LearningAnalysisInvalidInput
        ),
    }
    if "exerciseKey" in value:
        subject["exerciseKey"] = require_text(
            value["exerciseKey"], "subject.exerciseKey", error_type=LearningAnalysisInvalidInput
        )
    return subject


def _source_key(value: Mapping[str, object]) -> tuple[str, int]:
    source = value["source"]
    if not isinstance(source, Mapping):
        raise LearningAnalysisInvalidInput("source is invalid")
    return str(source["session"]), int(source["sequence"])


def _job_data(evidence: ReplicatedRecord, decoded: Mapping[str, object]) -> Mapping[str, object]:
    subject = decoded["subject"]
    basis = {
        "evidence": [
            {
                "after": None if evidence.parent is None else evidence.parent.to_value(),
                "through": evidence.position.to_value(),
            }
        ]
    }
    context = {
        "schema": "learning.course-context",
        "version": 1,
        "data": subject,
    }
    frozen = {
        "subject": subject,
        "analysis": ANALYSIS,
        "basis": basis,
        "inputs": [
            {"role": "evidence", "record": evidence.position.to_value()},
            {"role": "course-context", "snapshot": context},
        ],
    }
    job_key = "sha256:" + sha256(_canonical(frozen)).hexdigest()
    return {
        "version": 1,
        "type": "learning.analysis.job.created",
        "jobKey": job_key,
        "source": decoded["source"],
        **frozen,
    }


def _decode_job(value: object) -> Mapping[str, object]:
    if not _is_job(value):
        raise LearningAnalysisInvalidInput("Job shape is invalid")
    return value


def _is_job(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value)
        == {"version", "type", "jobKey", "source", "subject", "analysis", "basis", "inputs"}
        and value.get("version") == 1
        and value.get("type") == "learning.analysis.job.created"
    )


def _decode_assessment(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"schema", "version", "data"}:
        raise LearningAnalysisInvalidInput("Analysis output envelope is invalid")
    if value.get("schema") != "learning.assessment" or value.get("version") != 1:
        raise LearningAnalysisInvalidInput("Analysis output schema or version is invalid")
    data = value.get("data")
    if not isinstance(data, Mapping) or set(data) != {
        "summary",
        "findings",
        "recommendations",
        "limitations",
    }:
        raise LearningAnalysisInvalidInput("Analysis assessment shape is invalid")
    require_text(data.get("summary"), "assessment.summary", error_type=LearningAnalysisInvalidInput)
    for field in ("findings", "recommendations", "limitations"):
        if not isinstance(data.get(field), (list, tuple)):
            raise LearningAnalysisInvalidInput(f"assessment.{field} must be an array")
    for index, finding in enumerate(data["findings"]):
        label = f"assessment.findings[{index}]"
        if not isinstance(finding, Mapping) or set(finding) != {
            "kind",
            "topic",
            "claim",
            "confidence",
        }:
            raise LearningAnalysisInvalidInput(f"{label} shape is invalid")
        if finding["kind"] not in {
            "strength",
            "difficulty",
            "misconception",
            "strategy",
            "progress",
            "uncertain",
        }:
            raise LearningAnalysisInvalidInput(f"{label}.kind is invalid")
        _decode_topic(finding["topic"], f"{label}.topic")
        require_text(finding["claim"], f"{label}.claim", error_type=LearningAnalysisInvalidInput)
        if finding["confidence"] not in {"low", "medium", "high"}:
            raise LearningAnalysisInvalidInput(f"{label}.confidence is invalid")
    for index, recommendation in enumerate(data["recommendations"]):
        label = f"assessment.recommendations[{index}]"
        if not isinstance(recommendation, Mapping) or set(recommendation) != {
            "action",
            "topic",
            "priority",
            "rationale",
        }:
            raise LearningAnalysisInvalidInput(f"{label} shape is invalid")
        if recommendation["action"] not in {"explain", "practice", "review", "challenge"}:
            raise LearningAnalysisInvalidInput(f"{label}.action is invalid")
        _decode_topic(recommendation["topic"], f"{label}.topic")
        if recommendation["priority"] not in {"now", "soon", "later"}:
            raise LearningAnalysisInvalidInput(f"{label}.priority is invalid")
        require_text(
            recommendation["rationale"],
            f"{label}.rationale",
            error_type=LearningAnalysisInvalidInput,
        )
    for index, limitation in enumerate(data["limitations"]):
        require_text(
            limitation, f"assessment.limitations[{index}]", error_type=LearningAnalysisInvalidInput
        )
    return value


def _decode_topic(value: object, label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {"key", "label"}:
        raise LearningAnalysisInvalidInput(f"{label} shape is invalid")
    require_text(value["key"], f"{label}.key", error_type=LearningAnalysisInvalidInput)
    require_text(value["label"], f"{label}.label", error_type=LearningAnalysisInvalidInput)


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
