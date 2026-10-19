from __future__ import annotations

import math
from collections.abc import Mapping

from apps.server.validation import require_text


class LearningAnalysisInvalidInput(ValueError):
    pass


MAX_ASSESSMENT_ITEMS = 8
MAX_ASSESSMENT_TEXT_LENGTH = 1024


def _exact(value: Mapping[str, object], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise LearningAnalysisInvalidInput(f"{label} shape is invalid")


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 2**53 - 1:
        raise LearningAnalysisInvalidInput(f"{label} must be a positive integer")
    return value


def _json(value: object, label: str, seen: set[int] | None = None) -> object:
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise LearningAnalysisInvalidInput(f"{label} must contain only finite numbers")
        return value
    if seen is None:
        seen = set()
    if not isinstance(value, (Mapping, list, tuple)):
        raise LearningAnalysisInvalidInput(f"{label} must be a JSON value")
    if id(value) in seen:
        raise LearningAnalysisInvalidInput(f"{label} must be an acyclic JSON value")
    seen.add(id(value))
    try:
        if isinstance(value, Mapping):
            if any(not isinstance(key, str) for key in value):
                raise LearningAnalysisInvalidInput(f"{label} must contain only JSON objects")
            return {key: _json(item, f"{label}.{key}", seen) for key, item in value.items()}
        return tuple(_json(item, f"{label}[{index}]", seen) for index, item in enumerate(value))
    finally:
        seen.remove(id(value))


def decode_evidence_data(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LearningAnalysisInvalidInput("Evidence must be an object")
    _exact(value, {"version", "type", "source", "subject", "payload"}, "Evidence")
    if value["version"] != 1 or value["type"] != "learning.evidence.recorded":
        raise LearningAnalysisInvalidInput("Evidence version or type is invalid")
    source = value["source"]
    if not isinstance(source, Mapping):
        raise LearningAnalysisInvalidInput("Evidence.source must be an object")
    _exact(source, {"session", "sequence"}, "Evidence.source")
    session = require_text(
        source.get("session"), "Evidence.source.session", error_type=LearningAnalysisInvalidInput
    )
    sequence = _positive_integer(source.get("sequence"), "Evidence.source.sequence")
    subject = _decode_subject(value["subject"])
    payload = value["payload"]
    if not isinstance(payload, Mapping):
        raise LearningAnalysisInvalidInput("Evidence.payload must be an object")
    _exact(payload, {"schema", "version", "data"}, "Evidence.payload")
    schema = require_text(
        payload.get("schema"), "Evidence.payload.schema", error_type=LearningAnalysisInvalidInput
    )
    version = _positive_integer(payload.get("version"), "Evidence.payload.version")
    return {
        "version": 1,
        "type": "learning.evidence.recorded",
        "source": {"session": session, "sequence": sequence},
        "subject": subject,
        "payload": {
            "schema": schema,
            "version": version,
            "data": _json(payload["data"], "Evidence.payload.data"),
        },
    }


def _decode_subject(value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise LearningAnalysisInvalidInput("subject must be an object")
    fields = {"playgroundId", "versionId"} | ({"exerciseKey"} if "exerciseKey" in value else set())
    _exact(value, fields, "subject")
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


def decode_assessment(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LearningAnalysisInvalidInput("Analysis output envelope is invalid")
    _exact(value, {"schema", "version", "data"}, "Analysis output envelope")
    if value.get("schema") != "learning.assessment" or value.get("version") != 1:
        raise LearningAnalysisInvalidInput("Analysis output schema or version is invalid")
    data = value.get("data")
    if not isinstance(data, Mapping):
        raise LearningAnalysisInvalidInput("Analysis assessment shape is invalid")
    _exact(data, {"summary", "findings", "recommendations", "limitations"}, "Analysis assessment")
    _assessment_text(data.get("summary"), "assessment.summary")
    for field in ("findings", "recommendations", "limitations"):
        if not isinstance(data.get(field), (list, tuple)):
            raise LearningAnalysisInvalidInput(f"assessment.{field} must be an array")
        if len(data[field]) > MAX_ASSESSMENT_ITEMS:
            raise LearningAnalysisInvalidInput(
                f"assessment.{field} must contain at most {MAX_ASSESSMENT_ITEMS} items"
            )
    for index, finding in enumerate(data["findings"]):
        label = f"assessment.findings[{index}]"
        if not isinstance(finding, Mapping):
            raise LearningAnalysisInvalidInput(f"{label} shape is invalid")
        _exact(finding, {"kind", "topic", "claim", "confidence"}, label)
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
        _assessment_text(finding["claim"], f"{label}.claim")
        if finding["confidence"] not in {"low", "medium", "high"}:
            raise LearningAnalysisInvalidInput(f"{label}.confidence is invalid")
    for index, recommendation in enumerate(data["recommendations"]):
        label = f"assessment.recommendations[{index}]"
        if not isinstance(recommendation, Mapping):
            raise LearningAnalysisInvalidInput(f"{label} shape is invalid")
        _exact(recommendation, {"action", "topic", "priority", "rationale"}, label)
        if recommendation["action"] not in {"explain", "practice", "review", "challenge"}:
            raise LearningAnalysisInvalidInput(f"{label}.action is invalid")
        _decode_topic(recommendation["topic"], f"{label}.topic")
        if recommendation["priority"] not in {"now", "soon", "later"}:
            raise LearningAnalysisInvalidInput(f"{label}.priority is invalid")
        _assessment_text(recommendation["rationale"], f"{label}.rationale")
    for index, limitation in enumerate(data["limitations"]):
        _assessment_text(limitation, f"assessment.limitations[{index}]")
    return value


def _assessment_text(value: object, label: str) -> str:
    text = require_text(value, label, error_type=LearningAnalysisInvalidInput)
    if len(text) > MAX_ASSESSMENT_TEXT_LENGTH:
        raise LearningAnalysisInvalidInput(
            f"{label} must contain at most {MAX_ASSESSMENT_TEXT_LENGTH} characters"
        )
    return text


def _decode_topic(value: object, label: str) -> None:
    if not isinstance(value, Mapping):
        raise LearningAnalysisInvalidInput(f"{label} shape is invalid")
    _exact(value, {"key", "label"}, label)
    _assessment_text(value["key"], f"{label}.key")
    _assessment_text(value["label"], f"{label}.label")
