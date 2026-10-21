import json
import os

from apps.server.learning_analysis.agent import OpenAILearningAgent
from apps.server.learning_analysis.harness import InMemoryEventStore, LearningAnalysisHarness
from apps.server.learning_analysis.mock_data import CATALOG, FIRST_ATTEMPT, PRACTICE_RESULT, RULES


def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("PICOPG_LEARNING_MODEL")
    if not api_key or not model:
        raise SystemExit("set OPENAI_API_KEY and PICOPG_LEARNING_MODEL")

    harness = LearningAnalysisHarness(
        InMemoryEventStore(), RULES, CATALOG, OpenAILearningAgent(api_key, model)
    )
    before = harness.record(FIRST_ATTEMPT)
    analysis = harness.analyze("student-1", "attempt-1", "理解循环边界")
    after = harness.record(PRACTICE_RESULT)
    print(
        json.dumps(
            {
                "profileBefore": before.to_dict(),
                "agentOutput": analysis.to_dict(),
                "profileAfter": after.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
