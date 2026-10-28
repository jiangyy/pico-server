from __future__ import annotations

from collections.abc import Mapping

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
