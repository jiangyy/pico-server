from __future__ import annotations

import json
from typing import NoReturn

from picopg.replica import InvalidReplicaData


def strict_json_loads(value: str) -> object:
    return json.loads(
        value,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidReplicaData(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise InvalidReplicaData(f"invalid JSON number {value}")
