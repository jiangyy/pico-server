"""OpenAI 兼容的聊天模型：一问一答（JSON 输出）与带工具的一步。"""
from __future__ import annotations

import json
from typing import Protocol
from urllib.request import Request, urlopen


class CourseAiError(Exception):
    pass


class ChatModel(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> str: ...


class ToolModel(Protocol):
    def step(self, messages: list[dict[str, object]], tools: list[dict[str, object]]) -> dict[str, object]: ...


class OpenAiCompatibleChatModel:
    def __init__(self, base_url: str, model: str, api_key: str | None = None, effort: str | None = None) -> None:
        if not base_url.startswith(("http://", "https://")) or not model:
            raise ValueError("AI base URL must be HTTP(S) and model must be non-empty")
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.model = model
        self.api_key = api_key
        self.effort = effort

    def complete(self, messages: list[dict[str, str]]) -> str:
        content = self._message({
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }, timeout=45).get("content")
        if not isinstance(content, str) or not content.strip():
            raise CourseAiError("model returned no content")
        return content

    def step(self, messages: list[dict[str, object]], tools: list[dict[str, object]]) -> dict[str, object]:
        """一次带工具的补全，返回 assistant 消息原样（content、tool_calls、reasoning_content）。"""
        request: dict[str, object] = {"messages": messages, "temperature": 0.3}
        if tools:
            request["tools"] = tools
        if self.effort:
            request["reasoning_effort"] = self.effort
        return self._message(request, timeout=120)

    def _message(self, request: dict[str, object], *, timeout: float) -> dict[str, object]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = json.dumps({"model": self.model, **request}, ensure_ascii=False).encode("utf-8")
        try:
            with urlopen(Request(self.url, body, headers, method="POST"), timeout=timeout) as response:
                message = json.loads(response.read().decode("utf-8"))["choices"][0]["message"]
        except Exception as error:
            raise CourseAiError("model request failed") from error
        if not isinstance(message, dict):
            raise CourseAiError("model returned no message")
        return message
