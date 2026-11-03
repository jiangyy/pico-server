from __future__ import annotations

import unittest

from picopg.server.ai.model import CourseAiError, FallbackModel


class FailingModel:
    """先按设定次数失败，再给出固定回复；调用计数证明回退真的换到了下一个模型。"""

    def __init__(self, name: str, failures: int, reply: str) -> None:
        self.name = name
        self.failures = failures
        self.reply = reply
        self.complete_calls = 0
        self.step_calls = 0

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.complete_calls += 1
        if self.complete_calls <= self.failures:
            raise CourseAiError(f"{self.name} down")
        return self.reply

    def step(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]],
        on_text: object = None,
    ) -> dict[str, object]:
        self.step_calls += 1
        if self.step_calls <= self.failures:
            raise CourseAiError(f"{self.name} down")
        return {"role": "assistant", "content": self.reply}


class FallbackModelTest(unittest.TestCase):
    def test_complete_falls_back_to_the_next_model(self) -> None:
        first = FailingModel("first", failures=1, reply="ok")
        second = FailingModel("second", failures=0, reply="ok")
        fallback = FallbackModel([first, second])
        assert fallback.complete([{"role": "user", "content": "hi"}]) == "ok"
        assert first.complete_calls == 1 and second.complete_calls == 1

    def test_all_failed_raises_the_last_error(self) -> None:
        fallback = FallbackModel([FailingModel("a", 1, "x"), FailingModel("b", 1, "x")])
        with self.assertRaises(CourseAiError):
            fallback.complete([{"role": "user", "content": "hi"}])

    def test_client_disconnect_is_not_a_model_failure(self) -> None:
        class Gone:
            def step(self, messages: list[dict[str, object]], tools: list[dict[str, object]], on_text: object = None) -> dict[str, object]:
                raise BrokenPipeError()

        with self.assertRaises(BrokenPipeError):
            FallbackModel([Gone()]).step([], [], None)


if __name__ == "__main__":
    unittest.main()
