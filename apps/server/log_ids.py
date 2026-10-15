from __future__ import annotations

from urllib.parse import quote, unquote

from apps.server.validation import require_text


_COMPONENT_SAFE = "-_.!~*'()"


def encode_component(value: object, label: str) -> str:
    return quote(require_text(value, label), safe=_COMPONENT_SAFE)


def decode_component(value: object, label: str) -> str:
    encoded = require_text(value, label)
    decoded = unquote(encoded)
    if encode_component(decoded, label) != encoded:
        raise ValueError(f"{label} is not canonically encoded")
    return decoded
