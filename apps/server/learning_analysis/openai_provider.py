from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


Transport = Callable[[Request, float], bytes]


def _send(request: Request, timeout: float) -> bytes:
    with urlopen(request, timeout=timeout) as response:
        return response.read()


class OpenAIStructuredLearningProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 120.0,
        transport: Transport = _send,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OpenAI API key must be non-empty")
        if not model.strip():
            raise ValueError("learning model must be non-empty")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OpenAI base URL must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("OpenAI base URL must not contain a query or fragment")
        if timeout <= 0:
            raise ValueError("provider timeout must be positive")
        self._api_key = api_key
        self._model = model
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._timeout = timeout
        self._transport = transport

    def generate(
        self,
        *,
        analysis: str,
        system: str,
        input: Mapping[str, object],
        output_schema: Mapping[str, object],
    ) -> Mapping[str, object]:
        schema_name = re.sub(r"[^A-Za-z0-9_-]", "_", analysis).strip("_")
        if not schema_name:
            raise ValueError("analysis name must contain a schema-name character")
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        _plain(input),
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": _plain(output_schema),
                },
            },
        }
        request = Request(
            self._endpoint,
            data=json.dumps(
                payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8"),
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
        try:
            response = json.loads(raw)
            choices = response["choices"]
            content = choices[0]["message"]["content"]
            output = json.loads(content)
        except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("OpenAI returned an invalid structured response") from error
        if not isinstance(output, Mapping):
            raise RuntimeError("OpenAI structured response must be an object")
        return output


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value
