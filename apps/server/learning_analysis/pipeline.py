from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol, cast

from apps.server.draft.protocol import decode_revision
from apps.server.learning_analysis.worker import LearningAnalysisInvalidInput
from apps.server.validation import require_position
from picopg.api import ReplicatedRecord


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


SYSTEM_PROMPT = """你是编程课程的学习反馈分析器。
只依据提供的课程上下文、学习证据和提交源码作答，不推测身份、背景或未观察到的能力。
把观察区分为 strength、difficulty、misconception、strategy、progress 或 uncertain。
证据不足时降低 confidence，并在 limitations 中说明；不要把一次提交概括为长期能力。
recommendations 必须是下一步可执行的 explain、practice、review 或 challenge。
使用简体中文，只返回符合 learning.assessment@1 的结构化数据。"""


OUTPUT_SCHEMA = cast(
    Mapping[str, object],
    _freeze(
        {
            "type": "object",
            "additionalProperties": False,
            "required": ("summary", "findings", "recommendations", "limitations"),
            "properties": {
                "summary": {"type": "string", "minLength": 1},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ("kind", "topic", "claim", "confidence"),
                        "properties": {
                            "kind": {
                                "enum": (
                                    "strength",
                                    "difficulty",
                                    "misconception",
                                    "strategy",
                                    "progress",
                                    "uncertain",
                                )
                            },
                            "topic": {"$ref": "#/$defs/topic"},
                            "claim": {"type": "string", "minLength": 1},
                            "confidence": {"enum": ("low", "medium", "high")},
                        },
                    },
                },
                "recommendations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ("action", "topic", "priority", "rationale"),
                        "properties": {
                            "action": {"enum": ("explain", "practice", "review", "challenge")},
                            "topic": {"$ref": "#/$defs/topic"},
                            "priority": {"enum": ("now", "soon", "later")},
                            "rationale": {"type": "string", "minLength": 1},
                        },
                    },
                },
                "limitations": {"type": "array", "items": {"type": "string", "minLength": 1}},
            },
            "$defs": {
                "topic": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ("key", "label"),
                    "properties": {
                        "key": {"type": "string", "minLength": 1},
                        "label": {"type": "string", "minLength": 1},
                    },
                }
            },
        }
    ),
)


class StructuredLearningProvider(Protocol):
    def generate(
        self,
        *,
        analysis: str,
        system: str,
        input: Mapping[str, object],
        output_schema: Mapping[str, object],
    ) -> Mapping[str, object]: ...


class LearningFeedbackAnalyzer:
    def __init__(self, provider: StructuredLearningProvider) -> None:
        self._provider = provider

    def analyze(
        self,
        *,
        job: Mapping[str, object],
        records: tuple[ReplicatedRecord, ...],
    ) -> Mapping[str, object]:
        analysis = job.get("analysis")
        if not isinstance(analysis, Mapping) or dict(analysis) != {
            "key": "learning.feedback",
            "revision": "1",
        }:
            raise LearningAnalysisInvalidInput("Analysis revision is unsupported")
        prompt_input = _prompt_input(job, records)
        assessment = self._provider.generate(
            analysis="learning.feedback@1",
            system=SYSTEM_PROMPT,
            input=prompt_input,
            output_schema=OUTPUT_SCHEMA,
        )
        if not isinstance(assessment, Mapping):
            raise LearningAnalysisInvalidInput("Provider output must be an object")
        return {
            "schema": "learning.assessment",
            "version": 1,
            "data": assessment,
        }


def _prompt_input(
    job: Mapping[str, object], records: tuple[ReplicatedRecord, ...]
) -> Mapping[str, object]:
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
                value["record"], f"inputs.{role}.record", error_type=LearningAnalysisInvalidInput
            )
            record = by_position.get((position.log_id, position.record_id))
            if record is None:
                raise LearningAnalysisInvalidInput(f"{role} record is missing")
            roles[role] = record
        elif "snapshot" in value:
            roles[role] = value["snapshot"]
        else:
            raise LearningAnalysisInvalidInput("Job input has no value")
    if set(roles) != {"evidence", "submission-request", "draft-revision", "course-context"}:
        raise LearningAnalysisInvalidInput("Job input roles are unsupported")

    evidence = _record_data(roles["evidence"], "Evidence")
    payload = evidence.get("payload")
    if not isinstance(payload, Mapping) or payload.get("schema") != "learning.submission.requested":
        raise LearningAnalysisInvalidInput("Evidence schema is unsupported")
    detail = payload.get("data")
    if not isinstance(detail, Mapping) or set(detail) != {"request"}:
        raise LearningAnalysisInvalidInput("Evidence data is invalid")
    request = _record_data(roles["submission-request"], "Submission request")
    if request.get("type") != "submission.requested":
        raise LearningAnalysisInvalidInput("Submission request type is invalid")
    revision_record = roles["draft-revision"]
    if not isinstance(revision_record, ReplicatedRecord):
        raise LearningAnalysisInvalidInput("Draft Revision is invalid")
    try:
        revision = decode_revision(revision_record)
    except ValueError as error:
        raise LearningAnalysisInvalidInput("Draft Revision is invalid") from error
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
    request_record = roles["submission-request"]
    assert isinstance(request_record, ReplicatedRecord)
    evidence_request = require_position(
        detail["request"], "evidence.request", error_type=LearningAnalysisInvalidInput
    )
    if evidence_request != request_record.position:
        raise LearningAnalysisInvalidInput("Evidence does not reference the Job submission")
    request_revision = require_position(
        request.get("revision"),
        "submission.revision",
        error_type=LearningAnalysisInvalidInput,
    )
    if request_revision != revision_record.position:
        raise LearningAnalysisInvalidInput("Submission does not reference the Job revision")
    if request.get("playgroundId") != subject.get("playgroundId"):
        raise LearningAnalysisInvalidInput("Submission does not match the Job subject")
    return _freeze(
        {
            "language": "zh-CN",
            "subject": subject,
            "courseContext": context["data"],
            "evidence": (
                {
                    "schema": "learning.submission.requested",
                    "version": 1,
                    "data": {"event": "submission-requested"},
                },
            ),
            "submission": {"source": revision["source"]},
        }
    )


def _record_data(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, ReplicatedRecord) or not isinstance(value.data, Mapping):
        raise LearningAnalysisInvalidInput(f"{label} is invalid")
    return value.data
