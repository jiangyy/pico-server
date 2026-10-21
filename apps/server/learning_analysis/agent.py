"""Agent contract and OpenAI implementation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
import json
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from apps.server.learning_analysis.core import LearnerProfile, LearningEvent

Purpose = Literal["diagnose", "teach", "practice", "transfer", "review"]
Transport = Callable[[Request, float], bytes]
MAX_RESPONSE_BYTES = 256 * 1024


@dataclass(frozen=True)
class LearningActivity:
    activity_id: str
    version: int
    title: str
    prompt: str
    purpose: Purpose
    skill_ids: tuple[str, ...]


@dataclass(frozen=True)
class AgentContext:
    profile: LearnerProfile
    current_activity: LearningActivity
    current_attempt_id: str
    events: tuple[LearningEvent, ...]
    learning_goal: str | None
    catalog: tuple[LearningActivity, ...]


@dataclass(frozen=True)
class Finding:
    text: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Guidance:
    text: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class LearningPlan:
    activity_id: str
    reason: str
    expected_observation: str


@dataclass(frozen=True)
class AgentOutput:
    current_level: str
    strengths: tuple[Finding, ...]
    needs: tuple[Finding, ...]
    open_questions: tuple[str, ...]
    guidance: Guidance
    plan: LearningPlan | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class LearningAgent(Protocol):
    def analyze(self, context: AgentContext) -> AgentOutput: ...


SYSTEM_PROMPT = """你是 Pico 编程学习 Agent。
输入包含当前活动、学生过程事件、程序生成的画像、学习目标和可选活动。
先区分观察事实与解释，再给一个当前编程动作和一个下一步计划。
优势、困难和指导必须引用输入中的 event_id。
计划只能选择 catalog 中的 activity_id；没有合适活动时返回 null。
不要修改画像，不要把未采集数据解释为失败，只返回约定 JSON。"""


OUTPUT_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "currentLevel",
        "strengths",
        "needs",
        "openQuestions",
        "guidance",
        "plan",
    ],
    "properties": {
        "currentLevel": {"type": "string"},
        "strengths": {"type": "array", "items": {"$ref": "#/$defs/finding"}},
        "needs": {"type": "array", "items": {"$ref": "#/$defs/finding"}},
        "openQuestions": {"type": "array", "items": {"type": "string"}},
        "guidance": {"$ref": "#/$defs/finding"},
        "plan": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["activityId", "reason", "expectedObservation"],
                    "properties": {
                        "activityId": {"type": "string"},
                        "reason": {"type": "string"},
                        "expectedObservation": {"type": "string"},
                    },
                },
            ]
        },
    },
    "$defs": {
        "finding": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text", "evidenceIds"],
            "properties": {
                "text": {"type": "string"},
                "evidenceIds": {"type": "array", "items": {"type": "string"}},
            },
        }
    },
}


def _send(request: Request, timeout: float) -> bytes:
    with urlopen(request, timeout=timeout) as response:
        return response.read(MAX_RESPONSE_BYTES + 1)


class OpenAILearningAgent:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 120.0,
        transport: Transport = _send,
    ) -> None:
        if not api_key or not model:
            raise ValueError("api_key and model are required")
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._transport = transport

    def analyze(self, context: AgentContext) -> AgentOutput:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(asdict(context), ensure_ascii=False),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "pico_learning_analysis",
                    "strict": True,
                    "schema": OUTPUT_SCHEMA,
                },
            },
        }
        request = Request(
            self._endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            raw = self._transport(request, self._timeout)
        except HTTPError as error:
            raise RuntimeError(f"OpenAI returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise RuntimeError("OpenAI request failed") from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("OpenAI response is too large")
        try:
            response = json.loads(raw)
            data = json.loads(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("OpenAI returned invalid structured output") from error
        return decode_agent_output(data)


def decode_agent_output(value: object) -> AgentOutput:
    if not isinstance(value, Mapping):
        raise ValueError("agent output must be an object")
    required = {
        "currentLevel", "strengths", "needs", "openQuestions", "guidance", "plan"
    }
    if set(value) != required:
        raise ValueError("agent output fields are invalid")
    questions = value["openQuestions"]
    if not isinstance(questions, list):
        raise ValueError("openQuestions must be an array")
    plan_value = value["plan"]
    plan = None if plan_value is None else _plan(plan_value)
    guidance = _finding(value["guidance"], "guidance")
    return AgentOutput(
        current_level=_text(value["currentLevel"], "currentLevel"),
        strengths=_findings(value["strengths"], "strengths"),
        needs=_findings(value["needs"], "needs"),
        open_questions=tuple(_text(item, "openQuestions item") for item in questions),
        guidance=Guidance(guidance.text, guidance.evidence_ids),
        plan=plan,
    )


def _findings(value: object, label: str) -> tuple[Finding, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return tuple(_finding(item, f"{label} item") for item in value)


def _finding(value: object, label: str) -> Finding:
    if not isinstance(value, Mapping) or set(value) != {"text", "evidenceIds"}:
        raise ValueError(f"{label} is invalid")
    evidence = value["evidenceIds"]
    if not isinstance(evidence, list):
        raise ValueError(f"{label}.evidenceIds must be an array")
    return Finding(
        _text(value["text"], f"{label}.text"),
        tuple(_text(item, f"{label}.evidenceIds item") for item in evidence),
    )


def _plan(value: object) -> LearningPlan:
    if not isinstance(value, Mapping) or set(value) != {
        "activityId", "reason", "expectedObservation"
    }:
        raise ValueError("plan is invalid")
    return LearningPlan(
        _text(value["activityId"], "plan.activityId"),
        _text(value["reason"], "plan.reason"),
        _text(value["expectedObservation"], "plan.expectedObservation"),
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value
