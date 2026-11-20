"""旧的按服务分的请求体，套上 /api/v1/ai/step 的信封；回答取出 turn.value。"""

from __future__ import annotations

STEP = "/api/v1/ai/step"


def envelope(prompt: str, value: dict[str, object]) -> dict[str, object]:
    course = {key: value[key] for key in ("playgroundId", "versionId") if key in value}
    rest = {key: item for key, item in value.items() if key not in course}
    return {"prompt": prompt, "course": course or None, "input": rest}


def answered(status: int, body: dict[str, object]) -> tuple[int, object]:
    return (status, body["turn"]["value"]) if status == 200 else (status, body)  # type: ignore[index]
