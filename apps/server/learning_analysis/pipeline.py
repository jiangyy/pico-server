from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol, cast

from apps.server.learning_analysis.materializers import (
    DEFAULT_MATERIALIZERS,
    LearningEvidenceMaterializer,
    materializer_for_job,
    materializer_map,
)
from apps.server.learning_analysis.protocol import LearningAnalysisInvalidInput
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
    def __init__(
        self,
        provider: StructuredLearningProvider,
        materializers: tuple[LearningEvidenceMaterializer, ...] = DEFAULT_MATERIALIZERS,
    ) -> None:
        self._provider = provider
        self._materializers = materializer_map(materializers)

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
        materializer = materializer_for_job(job, records, self._materializers)
        prompt_input = materializer.prompt_input(job=job, records=records)
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
