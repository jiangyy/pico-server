"""Principal-scoped Log permissions for replica sessions."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class PrincipalSession:
    principal_id: str
    replica_id: str
    token: str
    grants: Mapping[str, str]

    @classmethod
    def create(
        cls,
        principal_id: str,
        replica_id: str,
        token: str,
        grants: Mapping[str, str],
    ) -> "PrincipalSession":
        for value, label in ((principal_id, "principalId"), (replica_id, "replicaId"), (token, "token")):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{label} must be non-empty")
        copied = dict(grants)
        if any(not isinstance(log_id, str) or not log_id for log_id in copied):
            raise ValueError("grant Log IDs must be non-empty")
        if any(access not in ("read", "append") for access in copied.values()):
            raise ValueError("grant access must be 'read' or 'append'")
        return cls(principal_id, replica_id, token, MappingProxyType(copied))

    def permits(self, log_id: str, action: str, method: str) -> bool:
        access = self.grants.get(log_id)
        if access is None:
            return False
        if method == "POST":
            return action == "records" and access == "append"
        if method in ("GET", "HEAD"):
            if action not in ("head", "has", "records"):
                return False
            if access == "read":
                return True
            return action in ("head", "has")
        return False

    @property
    def readable_logs(self) -> frozenset[str]:
        # Append-only clients still need head invalidation events for their own Logs.
        return frozenset(self.grants)
