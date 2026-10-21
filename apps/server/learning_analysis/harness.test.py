from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from apps.server.learning_analysis.agent import (  # noqa: E402
    AgentOutput,
    Finding,
    Guidance,
    LearningPlan,
    OpenAILearningAgent,
)
from apps.server.learning_analysis.harness import (  # noqa: E402
    InMemoryEventStore,
    LearningAnalysisHarness,
)
from apps.server.learning_analysis.mock_data import (  # noqa: E402
    CATALOG,
    FIRST_ATTEMPT,
    PRACTICE_RESULT,
    RULES,
)


class FixedAgent:
    def analyze(self, context):
        return AgentOutput(
            "循环边界尚不稳定",
            (Finding("range 基础用法正确", ("test-3",)),),
            (Finding("n=1 测试失败", ("test-2",)),),
            (),
            Guidance("检查 range 的结束值。", ("test-2",)),
            LearningPlan("loop-boundary-practice", "练习边界", "无提示通过 n=1"),
        )


def harness_flow() -> None:
    harness = LearningAnalysisHarness(InMemoryEventStore(), RULES, CATALOG, FixedAgent())
    before = harness.record(FIRST_ATTEMPT)
    boundary = next(item for item in before.skills if item.skill_id == "python.loop.boundary")
    assert boundary.outcomes.partial == 1
    assert harness.analyze("student-1", "attempt-1").guidance.evidence_ids == ("test-2",)

    harness.record(FIRST_ATTEMPT)
    after = harness.record(PRACTICE_RESULT)
    boundary = next(item for item in after.skills if item.skill_id == "python.loop.boundary")
    assert boundary.attempts == 2
    assert boundary.outcomes.success == 1


def openai_request() -> None:
    def transport(request, timeout):
        output = {
            "currentLevel": "循环边界尚不稳定",
            "strengths": [{"text": "range 用法正确", "evidenceIds": ["test-3"]}],
            "needs": [{"text": "n=1 失败", "evidenceIds": ["test-2"]}],
            "openQuestions": [],
            "guidance": {"text": "检查结束值。", "evidenceIds": ["test-2"]},
            "plan": {
                "activityId": "loop-boundary-practice",
                "reason": "练习边界",
                "expectedObservation": "无提示通过 n=1",
            },
        }
        return json.dumps(
            {"choices": [{"message": {"content": json.dumps(output)}}]}
        ).encode()

    harness = LearningAnalysisHarness(
        InMemoryEventStore(),
        RULES,
        CATALOG,
        OpenAILearningAgent("test-key", "test-model", transport=transport),
    )
    harness.record(FIRST_ATTEMPT)
    assert harness.analyze("student-1", "attempt-1").needs[0].evidence_ids == ("test-2",)


harness_flow()
openai_request()
