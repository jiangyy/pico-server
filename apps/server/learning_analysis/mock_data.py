"""One complete event flow for local execution and tests."""

from apps.server.learning_analysis.agent import LearningActivity
from apps.server.learning_analysis.core import LearningEvent, SkillRule

CATALOG = (
    LearningActivity(
        "loop-03",
        1,
        "修复循环边界",
        "修改程序，使它从 1 输出到 n，包含 n。",
        "diagnose",
        ("python.loop.range-args", "python.loop.boundary"),
    ),
    LearningActivity(
        "loop-boundary-practice",
        1,
        "循环边界练习",
        "补全程序，并分别验证 n=5 和 n=1。",
        "practice",
        ("python.loop.boundary",),
    ),
)

RULES = (
    SkillRule("loop-03", 1, "python.loop.boundary", ("n-5", "n-1")),
    SkillRule("loop-03", 1, "python.loop.range-args", ("n-5",)),
    SkillRule(
        "loop-boundary-practice", 1, "python.loop.boundary", ("n-5", "n-1")
    ),
)

FIRST_ATTEMPT = (
    LearningEvent(
        "edit-1", "student-1", "attempt-1", "loop-03", 1, 1, "code.saved",
        source="for i in range(1, n): print(i)",
    ),
    LearningEvent(
        "run-1", "student-1", "attempt-1", "loop-03", 1, 2, "run.finished",
        run_output="1\n2\n3\n4",
    ),
    LearningEvent(
        "test-1", "student-1", "attempt-1", "loop-03", 1, 3, "test.finished",
        skill_id="python.loop.boundary", case_id="n-5", test_result="pass",
    ),
    LearningEvent(
        "test-2", "student-1", "attempt-1", "loop-03", 1, 4, "test.finished",
        skill_id="python.loop.boundary", case_id="n-1", test_result="fail",
    ),
    LearningEvent(
        "test-3", "student-1", "attempt-1", "loop-03", 1, 5, "test.finished",
        skill_id="python.loop.range-args", case_id="n-5", test_result="pass",
    ),
)

PRACTICE_RESULT = (
    LearningEvent(
        "activity-1", "student-1", "attempt-2", "loop-boundary-practice", 1, 1,
        "activity.finished",
    ),
    LearningEvent(
        "test-4", "student-1", "attempt-2", "loop-boundary-practice", 1, 2,
        "test.finished", skill_id="python.loop.boundary", case_id="n-5",
        test_result="pass",
    ),
    LearningEvent(
        "test-5", "student-1", "attempt-2", "loop-boundary-practice", 1, 3,
        "test.finished", skill_id="python.loop.boundary", case_id="n-1",
        test_result="pass",
    ),
)
