"""Which principals may send their code to the configured model; nobody unless the operator says so."""

from __future__ import annotations

from collections.abc import Iterable


class AiAccess:
    def __init__(self, principals: Iterable[str] = (), everyone: bool = False) -> None:
        self.principals = frozenset(principals)
        self.everyone = everyone

    @classmethod
    def from_setting(cls, value: str | None) -> "AiAccess":
        """PICOPG_AI_PRINCIPALS: `*` for everyone, or comma-separated principal IDs."""
        entries = [entry.strip() for entry in (value or "").split(",") if entry.strip()]
        if "*" in entries:
            if len(entries) > 1:
                raise ValueError("PICOPG_AI_PRINCIPALS is either * or a list of principal IDs")
            return cls(everyone=True)
        return cls(entries)

    def allows(self, principal_id: str) -> bool:
        return self.everyone or principal_id in self.principals
