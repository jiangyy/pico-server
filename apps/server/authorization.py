"""Principal-scoped Log permissions for replica sessions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping
from urllib.parse import quote


PRINCIPAL_CAPABILITIES = frozenset(("playgrounds", "drafts", "submissions"))


@dataclass(frozen=True, slots=True)
class PrincipalSession:
    principal_id: str
    replica_id: str
    token: str
    grants: Mapping[str, str]
    capabilities: frozenset[str]

    @classmethod
    def create(
        cls,
        principal_id: str,
        replica_id: str,
        token: str,
        grants: Mapping[str, str],
        capabilities: Iterable[str] = (),
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
        enabled = frozenset(capabilities)
        unknown = enabled - PRINCIPAL_CAPABILITIES
        if unknown:
            raise ValueError(f"unknown principal capabilities: {', '.join(sorted(unknown))}")
        return cls(principal_id, replica_id, token, MappingProxyType(copied), enabled)

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


def _dynamic_access(
    session: PrincipalSession,
    sessions: Iterable[PrincipalSession],
    log_id: str,
) -> str | None:
    if "playgrounds" in session.capabilities and log_id == "playground-releases":
        return "read"
    parts = log_id.split("/")
    owner = quote(session.replica_id, safe="-_.!~*'()")
    if "drafts" in session.capabilities and len(parts) >= 3 and parts[0] == "draft" and parts[1]:
        if len(parts) == 3 and parts[2] in ("decision", "comment"):
            return "read"
        if len(parts) == 4 and parts[3]:
            if parts[2] == "request":
                return "append" if parts[3] == owner else None
            if parts[2] == "revision":
                if parts[3] == owner:
                    return "append"
                principal_replicas = {
                    quote(candidate.replica_id, safe="-_.!~*'()")
                    for candidate in sessions
                    if candidate.principal_id == session.principal_id
                    and "drafts" in candidate.capabilities
                }
                return "read" if parts[3] in principal_replicas else None
    if "submissions" in session.capabilities and len(parts) == 3 and parts[0] == "submission":
        if parts[1] != owner:
            return None
        if parts[2] == "requests":
            return "append"
        if parts[2] == "results":
            return "read"
    return None


def _permits(access: str | None, action: str, method: str) -> bool:
    if access is None:
        return False
    if method == "POST":
        return action == "records" and access == "append"
    if method in ("GET", "HEAD") and action in ("head", "has", "records"):
        return access == "read" or action in ("head", "has")
    return False


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
    known = tuple(sessions)
    if session.permits(log_id, action, method) or _permits(
        _dynamic_access(session, known, log_id), action, method
    ):
        return True
    return (
        method in ("GET", "HEAD")
        and action in ("head", "has", "records")
        and log_id in readable_principal_logs(session, known)
    )


def principal_observes_log(
    session: PrincipalSession,
    sessions: Iterable[PrincipalSession],
    log_id: str,
) -> bool:
    return permits_principal_log(session, sessions, log_id, "head", "HEAD")
