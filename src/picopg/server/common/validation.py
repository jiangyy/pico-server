from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from picopg.replica import Position


def require_text(
    value: object,
    label: str,
    *,
    reject_whitespace: bool = False,
    error_type: type[Exception] = ValueError,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or (reject_whitespace and not value.strip())
    ):
        raise error_type(f"{label} must be non-empty")
    return value


def require_position(
    value: object,
    label: str = "position",
    *,
    reject_whitespace: bool = False,
    error_type: type[Exception] = ValueError,
) -> Position:
    if not isinstance(value, Mapping) or set(value) != {"logId", "recordId"}:
        raise error_type(f"{label} must contain logId and recordId")
    return Position(
        require_text(
            value["logId"],
            f"{label}.logId",
            reject_whitespace=reject_whitespace,
            error_type=error_type,
        ),
        require_text(
            value["recordId"],
            f"{label}.recordId",
            reject_whitespace=reject_whitespace,
            error_type=error_type,
        ),
    )


def require_non_blank_text(
    value: object,
    label: str,
    *,
    error_type: type[Exception] = ValueError,
) -> str:
    return require_text(
        value,
        label,
        reject_whitespace=True,
        error_type=error_type,
    )


def require_non_blank_position(
    value: object,
    label: str = "position",
    *,
    error_type: type[Exception] = ValueError,
) -> Position:
    return require_position(
        value,
        label,
        reject_whitespace=True,
        error_type=error_type,
    )


def optional_time(value: object, name: str, *, error_type: type[ValueError] = ValueError) -> str | None:
    """事实里可选的 ISO 8601 时间：旧记录没有它；有就必须带时区、能解析。原样返回，不改写。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise error_type(f"{name} must be an ISO 8601 time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise error_type(f"{name} must be an ISO 8601 time") from error
    if parsed.tzinfo is None:
        raise error_type(f"{name} must carry a time zone")
    return value


def utc_now() -> str:
    """写进事实的服务端时间：UTC、毫秒、以 Z 结尾，与客户端 toISOString() 同一格式。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
