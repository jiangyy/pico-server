"""Principal-scoped Log permissions for replica sessions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping
from urllib.parse import quote


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
        owner = quote(replica_id, safe="-_.!~*'()")
        for log_id, access in copied.items():
            if access == "append" and _replica_owner(log_id) not in (None, owner):
                raise ValueError(f"append grant {log_id!r} belongs to another Replica")
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


def _replica_owner(log_id: str) -> str | None:
    parts = log_id.split("/")
    if len(parts) == 4 and parts[0] == "draft" and parts[2] in ("revision", "request"):
        return parts[3]
    if len(parts) == 3 and parts[0] == "submission" and parts[2] == "requests":
        return parts[1]
    return None


def readable_principal_logs(
    session: PrincipalSession, sessions: Iterable[PrincipalSession]
) -> frozenset[str]:
    readable = set(session.readable_logs)
    for candidate in sessions:
        if candidate.principal_id != session.principal_id:
            continue
        owner = quote(candidate.replica_id, safe="-_.!~*'()")
        for log_id, access in candidate.grants.items():
            parts = log_id.split("/")
            if (
                access == "append"
                and len(parts) == 4
                and parts[0] == "draft"
                and parts[2] == "revision"
                and parts[3] == owner
                and session.grants.get(f"draft/{parts[1]}/decision") == "read"
            ):
                readable.add(log_id)
    return frozenset(readable)


def permits_principal_log(
    session: PrincipalSession,
    sessions: Iterable[PrincipalSession],
    log_id: str,
    action: str,
    method: str,
) -> bool:
    if session.permits(log_id, action, method):
        return True
    return (
        method in ("GET", "HEAD")
        and action in ("head", "has", "records")
        and log_id in readable_principal_logs(session, sessions)
    )
