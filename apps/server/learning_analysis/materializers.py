from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from apps.server.learning_analysis.protocol import (
    LearningAnalysisInvalidInput,
    decode_evidence_data,
)
from apps.server.log_reads import find_record
from apps.server.submission.logs import request_scope
from apps.server.submission.protocol import (
    SubmissionError,
    decode_request,
    read_source,
    source_from_revision,
)
from apps.server.validation import require_position
from picopg.api import Position, ReplicaLogStore, ReplicatedRecord


@dataclass(frozen=True)
class MaterializedEvidence:
    inputs: tuple[Mapping[str, object], ...]
    dependencies: tuple[Position, ...]


class LearningEvidenceMaterializer(Protocol):
    payload_schema: str
    payload_version: int

    def materialize(
        self,
        *,
        store: ReplicaLogStore,
        evidence: ReplicatedRecord,
        decoded: Mapping[str, object],
        principal_id: str,
    ) -> MaterializedEvidence: ...

    def prompt_input(
        self,
        *,
        job: Mapping[str, object],
        records: tuple[ReplicatedRecord, ...],
    ) -> Mapping[str, object]: ...


class SubmissionMaterializer:
    payload_schema = "learning.submission.requested"
    payload_version = 1

    def materialize(
        self,
        *,
        store: ReplicaLogStore,
        evidence: ReplicatedRecord,
        decoded: Mapping[str, object],
        principal_id: str,
    ) -> MaterializedEvidence:
        payload = decoded["payload"]
        assert isinstance(payload, Mapping)
        detail = payload["data"]
        if not isinstance(detail, Mapping) or set(detail) != {"request"}:
            raise LearningAnalysisInvalidInput("Submission Evidence data is invalid")
        request = require_position(
            detail["request"],
            "payload.data.request",
            error_type=LearningAnalysisInvalidInput,
        )
        if request not in evidence.requires:
            raise LearningAnalysisInvalidInput("Submission Evidence does not require its request")
        try:
            request_principal, _ = request_scope(request.log_id)
        except ValueError as error:
            raise LearningAnalysisInvalidInput("Submission request Log is invalid") from error
        if request_principal != principal_id:
            raise LearningAnalysisInvalidInput("Submission belongs to another Principal")

        request_record = find_record(store, request)
        if request_record is None:
            raise LearningAnalysisInvalidInput("Submission request is missing")
        try:
            submission = decode_request(request_record)
        except SubmissionError as error:
            raise LearningAnalysisInvalidInput(str(error)) from error
        subject = decoded["subject"]
        assert isinstance(subject, Mapping)
        if submission.playground_id != subject["playgroundId"]:
            raise LearningAnalysisInvalidInput("Submission belongs to another Playground")
        try:
            read_source(store, submission)
        except SubmissionError as error:
            raise LearningAnalysisInvalidInput(str(error)) from error
        revision = submission.revision
        return MaterializedEvidence(
            inputs=(
                {"role": "submission-request", "record": request.to_value()},
                {"role": "draft-revision", "record": revision.to_value()},
            ),
            dependencies=(request, revision),
        )

    def prompt_input(
        self,
        *,
        job: Mapping[str, object],
        records: tuple[ReplicatedRecord, ...],
    ) -> Mapping[str, object]:
        roles = _roles(job, records)
        if set(roles) != {
            "evidence",
            "submission-request",
            "draft-revision",
            "course-context",
        }:
            raise LearningAnalysisInvalidInput("Job input roles are unsupported")

        evidence = _record_data(roles["evidence"], "Evidence")
        payload = evidence.get("payload")
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema") != self.payload_schema
            or payload.get("version") != self.payload_version
        ):
            raise LearningAnalysisInvalidInput("Evidence schema is unsupported")
        detail = payload.get("data")
        if not isinstance(detail, Mapping) or set(detail) != {"request"}:
            raise LearningAnalysisInvalidInput("Evidence data is invalid")
        request_record = roles["submission-request"]
        if not isinstance(request_record, ReplicatedRecord):
            raise LearningAnalysisInvalidInput("Submission request is invalid")
        revision_record = roles["draft-revision"]
        if not isinstance(revision_record, ReplicatedRecord):
            raise LearningAnalysisInvalidInput("Draft Revision is invalid")
        try:
            submission = decode_request(request_record)
            source = source_from_revision(submission, revision_record)
        except SubmissionError as error:
            raise LearningAnalysisInvalidInput(str(error)) from error
        context = roles["course-context"]
        if not isinstance(context, Mapping) or set(context) != {"schema", "version", "data"}:
            raise LearningAnalysisInvalidInput("Course context is invalid")
        if context.get("schema") != "learning.course-context" or context.get("version") != 1:
            raise LearningAnalysisInvalidInput("Course context version is unsupported")
        subject = job.get("subject")
        if not isinstance(subject, Mapping) or context.get("data") != subject:
            raise LearningAnalysisInvalidInput("Course context does not match the Job subject")
        if evidence.get("subject") != subject:
            raise LearningAnalysisInvalidInput("Evidence does not match the Job subject")
        evidence_request = require_position(
            detail["request"], "evidence.request", error_type=LearningAnalysisInvalidInput
        )
        if evidence_request != request_record.position:
            raise LearningAnalysisInvalidInput("Evidence does not reference the Job submission")
        if submission.playground_id != subject.get("playgroundId"):
            raise LearningAnalysisInvalidInput("Submission does not match the Job subject")
        return _freeze(
            {
                "language": "zh-CN",
                "subject": subject,
                "courseContext": context["data"],
                "evidence": (
                    {
                        "schema": self.payload_schema,
                        "version": self.payload_version,
                        "data": {"event": "submission-requested"},
                    },
                ),
                "submission": {"source": source},
            }
        )


DEFAULT_MATERIALIZERS: tuple[LearningEvidenceMaterializer, ...] = (
    SubmissionMaterializer(),
)


def materializer_map(
    materializers: tuple[LearningEvidenceMaterializer, ...],
) -> Mapping[tuple[str, int], LearningEvidenceMaterializer]:
    mapped = {
        (materializer.payload_schema, materializer.payload_version): materializer
        for materializer in materializers
    }
    if len(mapped) != len(materializers):
        raise ValueError("materializer payload versions must be unique")
    return MappingProxyType(mapped)


def resolve_materializer(
    decoded: Mapping[str, object],
    materializers: Mapping[tuple[str, int], LearningEvidenceMaterializer],
) -> LearningEvidenceMaterializer:
    payload = decoded.get("payload")
    if not isinstance(payload, Mapping):
        raise LearningAnalysisInvalidInput("Evidence payload is invalid")
    schema = payload.get("schema")
    version = payload.get("version")
    if not isinstance(schema, str) or isinstance(version, bool) or not isinstance(version, int):
        raise LearningAnalysisInvalidInput("Evidence payload version is invalid")
    materializer = materializers.get((schema, version))
    if materializer is None:
        raise LearningAnalysisInvalidInput("Evidence payload schema is unsupported")
    return materializer


def materializer_for_job(
    job: Mapping[str, object],
    records: tuple[ReplicatedRecord, ...],
    materializers: Mapping[tuple[str, int], LearningEvidenceMaterializer],
) -> LearningEvidenceMaterializer:
    roles = _roles(job, records)
    evidence = _record_data(roles.get("evidence"), "Evidence")
    return resolve_materializer(decode_evidence_data(evidence), materializers)


def _roles(
    job: Mapping[str, object], records: tuple[ReplicatedRecord, ...]
) -> dict[str, object]:
    inputs = job.get("inputs")
    if not isinstance(inputs, tuple):
        raise LearningAnalysisInvalidInput("Job inputs are invalid")
    by_position = {(record.log_id, record.id): record for record in records}
    roles: dict[str, object] = {}
    for value in inputs:
        if not isinstance(value, Mapping):
            raise LearningAnalysisInvalidInput("Job input is invalid")
        role = value.get("role")
        if not isinstance(role, str) or role in roles:
            raise LearningAnalysisInvalidInput("Job input role is invalid")
        if "record" in value:
            position = require_position(
                value["record"],
                f"inputs.{role}.record",
                error_type=LearningAnalysisInvalidInput,
            )
            record = by_position.get((position.log_id, position.record_id))
            if record is None:
                raise LearningAnalysisInvalidInput(f"{role} record is missing")
            roles[role] = record
        elif "snapshot" in value:
            roles[role] = value["snapshot"]
        else:
            raise LearningAnalysisInvalidInput("Job input has no value")
    return roles


def _record_data(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, ReplicatedRecord) or not isinstance(value.data, Mapping):
        raise LearningAnalysisInvalidInput(f"{label} is invalid")
    return value.data


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value
