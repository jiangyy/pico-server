"""OpenAI 兼容的聊天模型：一问一答（JSON 输出）与带工具的一步。"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol
from urllib.request import Request, urlopen


class CourseAiError(Exception):
    pass


class ChatModel(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> str: ...


class ToolModel(Protocol):
    def step(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]],
        on_text: Callable[[str], None] | None = None,
    ) -> dict[str, object]: ...


class _Interrupted(Exception):
    """on_text 自己抛出的异常（例如客户端断开），原样交还调用方，不算模型失败。"""

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


class OpenAiCompatibleChatModel:
    def __init__(self, base_url: str, model: str, api_key: str | None = None, effort: str | None = None) -> None:
        if not base_url.startswith(("http://", "https://")) or not model:
            raise ValueError("AI base URL must be HTTP(S) and model must be non-empty")
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.model = model
        self.api_key = api_key
        self.effort = effort
        # 每次补全用掉的 token 数，交给费用上限记账。
        self.on_usage: Callable[[int], None] | None = None

    def complete(self, messages: list[dict[str, str]]) -> str:
        content = self._message({
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }, timeout=45).get("content")
        if not isinstance(content, str) or not content.strip():
            raise CourseAiError("model returned no content")
        return content

    def step(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]],
        on_text: Callable[[str], None] | None = None,
    ) -> dict[str, object]:
        """一次带工具的补全，返回 assistant 消息（content、tool_calls、reasoning_content）。
        给了 on_text 时流式请求，正文每到一段就交给它。"""
        request: dict[str, object] = {"messages": messages, "temperature": 0.3}
        if tools:
            request["tools"] = tools
        if self.effort:
            request["reasoning_effort"] = self.effort
        if on_text is None:
            return self._message(request, timeout=120)
        try:
            return self._stream(request, on_text, timeout=120)
        except _Interrupted as interrupted:
            raise interrupted.error from None

    def _request(self, request: dict[str, object]) -> Request:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = json.dumps({"model": self.model, **request}, ensure_ascii=False).encode("utf-8")
        return Request(self.url, body, headers, method="POST")

    def _spent(self, usage: object) -> None:
        if self.on_usage is not None and isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
            self.on_usage(usage["total_tokens"])

    def _message(self, request: dict[str, object], *, timeout: float) -> dict[str, object]:
        try:
            with urlopen(self._request(request), timeout=timeout) as response:
                reply = json.loads(response.read().decode("utf-8"))
            message = reply["choices"][0]["message"]
        except Exception as error:
            raise CourseAiError("model request failed") from error
        self._spent(reply.get("usage"))
        if not isinstance(message, dict):
            raise CourseAiError("model returned no message")
        if isinstance(reply.get("usage"), dict):
            message["usage"] = reply["usage"]
        return message

    def _stream(self, request: dict[str, object], on_text: Callable[[str], None], *, timeout: float) -> dict[str, object]:
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, object]] = {}
        usage: dict[str, object] | None = None
        try:
            with urlopen(self._request({**request, "stream": True, "stream_options": {"include_usage": True}}), timeout=timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    self._spent(chunk.get("usage"))
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            content.append(text)
                            try:
                                on_text(text)
                            except Exception as error:
                                raise _Interrupted(error) from error
                        thought = delta.get("reasoning_content")
                        if isinstance(thought, str):
                            reasoning.append(thought)
                        for call in delta.get("tool_calls") or []:
                            _merge_call(calls, call)
        except _Interrupted:
            raise
        except Exception as error:
            raise CourseAiError("model request failed") from error
        message: dict[str, object] = {"role": "assistant", "content": "".join(content)}
        if calls:
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if usage is not None:
            message["usage"] = usage
        return message


def _merge_call(calls: dict[int, dict[str, object]], delta: dict[str, object]) -> None:
    """流式的工具调用按 index 分片到达：id 与名字给一次，参数一段段拼起来。"""
    slot = calls.setdefault(int(delta.get("index") or 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    if isinstance(delta.get("id"), str):
        slot["id"] = delta["id"]
    function = delta.get("function") or {}
    target = slot["function"]
    assert isinstance(target, dict)
    for key in ("name", "arguments"):
        if isinstance(function.get(key), str):
            target[key] += function[key]


class FallbackModel:
    """按顺序尝试多个模型：一个报 CourseAiError（连不上、没返回内容）就换下一个。

    # ponytail: 流式 step 中途失败时，on_text 已经发出的片段收不回，下一个模型会重讲一遍；
    # 主流故障（503、连不上）都发生在第一个字节之前，这个上限先接受。
    """

    def __init__(self, models: list[OpenAiCompatibleChatModel]) -> None:
        if not models:
            raise ValueError("fallback needs at least one model")
        self.models = models
        # 最近一次是哪个模型答的；评测结果按它记模型。
        self.answered: str | None = None

    def complete(self, messages: list[dict[str, str]]) -> str:
        last: CourseAiError | None = None
        for model in self.models:
            try:
                content = model.complete(messages)
            except CourseAiError as error:
                last = error
                continue
            self.answered = model.model
            return content
        raise CourseAiError("all configured models failed") from last

    def step(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        on_text: Callable[[str], None] | None = None,
    ) -> dict[str, object]:
        last: CourseAiError | None = None
        for model in self.models:
            try:
                message = model.step(messages, tools, on_text)
            except CourseAiError as error:
                last = error
                continue
            self.answered = model.model
            return message
        raise CourseAiError("all configured models failed") from last
