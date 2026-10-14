"""Authentication and principal-scoped Log authorization."""

from __future__ import annotations

import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Protocol
from urllib.parse import quote


PRINCIPAL_CAPABILITIES = frozenset(("playgrounds", "drafts", "submissions"))


@dataclass(frozen=True, slots=True)
class AuthenticatedReplica:
    principal_id: str
    replica_id: str


class Authenticator(Protocol):
    def authenticate(self, token: str) -> AuthenticatedReplica | None: ...


class ReplicaDirectory(Protocol):
    def replicas(self, principal_id: str) -> frozenset[str]: ...


class LogAuthorization(Protocol):
    def access(self, identity: AuthenticatedReplica, log_id: str) -> str | None: ...

    def has_access(self, identity: AuthenticatedReplica) -> bool: ...


@dataclass(frozen=True, slots=True)
class PrincipalAccount:
    principal_id: str
    access_code: str
    capabilities: frozenset[str]

    @classmethod
    def create(
        cls,
        principal_id: str,
        access_code: str,
        capabilities: Iterable[str] = (),
    ) -> "PrincipalAccount":
        for value, label in ((principal_id, "principalId"), (access_code, "accessCode")):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{label} must be non-empty")
        enabled = frozenset(capabilities)
        unknown = enabled - PRINCIPAL_CAPABILITIES
        if unknown:
            raise ValueError(f"unknown principal capabilities: {', '.join(sorted(unknown))}")
        return cls(principal_id, access_code, enabled)


@dataclass(frozen=True, slots=True)
class PrincipalSession:
    """Static composition-root configuration; request handlers never receive it."""

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
        identity = AuthenticatedReplica(principal_id, replica_id)
        for log_id, access in copied.items():
            owner = _replica_owner(log_id)
            if access == "append" and owner is not None and owner != _encoded_identity(identity):
                raise ValueError(f"append grant {log_id!r} belongs to another Replica")
        enabled = frozenset(capabilities)
        unknown = enabled - PRINCIPAL_CAPABILITIES
        if unknown:
            raise ValueError(f"unknown principal capabilities: {', '.join(sorted(unknown))}")
        return cls(principal_id, replica_id, token, MappingProxyType(copied), enabled)


class StaticAuthenticator:
    def __init__(self, sessions: Iterable[PrincipalSession]) -> None:
        entries = tuple(sessions)
        if len({entry.token for entry in entries}) != len(entries):
            raise ValueError("principal session tokens must be unique")
        self._entries = tuple(
            (entry.token, AuthenticatedReplica(entry.principal_id, entry.replica_id))
            for entry in entries
        )

    def authenticate(self, token: str) -> AuthenticatedReplica | None:
        matched = None
        for known, identity in self._entries:
            if hmac.compare_digest(token, known):
                matched = identity
        return matched


class StaticReplicaDirectory:
    def __init__(self, sessions: Iterable[PrincipalSession]) -> None:
        by_principal: dict[str, set[str]] = {}
        identities: set[tuple[str, str]] = set()
        for session in sessions:
            key = (session.principal_id, session.replica_id)
            if key in identities:
                raise ValueError("principal session identities must be unique")
            identities.add(key)
            by_principal.setdefault(session.principal_id, set()).add(session.replica_id)
        self._by_principal = {
            principal_id: frozenset(replicas)
            for principal_id, replicas in by_principal.items()
        }

    def replicas(self, principal_id: str) -> frozenset[str]:
        return self._by_principal.get(principal_id, frozenset())


class CompositeAuthenticator:
    def __init__(self, authenticators: Iterable[Authenticator]) -> None:
        self._authenticators = tuple(authenticators)

    def authenticate(self, token: str) -> AuthenticatedReplica | None:
        for authenticator in self._authenticators:
            identity = authenticator.authenticate(token)
            if identity is not None:
                return identity
        return None


class CompositeReplicaDirectory:
    def __init__(self, directories: Iterable[ReplicaDirectory]) -> None:
        self._directories = tuple(directories)

    def replicas(self, principal_id: str) -> frozenset[str]:
        return frozenset(
            replica
            for directory in self._directories
            for replica in directory.replicas(principal_id)
        )


class PrincipalLogAuthorization:
    def __init__(
        self,
        sessions: Iterable[PrincipalSession],
        directory: ReplicaDirectory,
        accounts: Iterable[PrincipalAccount] = (),
    ) -> None:
        self._directory = directory
        self._grants: dict[AuthenticatedReplica, Mapping[str, str]] = {}
        self._capabilities: dict[AuthenticatedReplica, frozenset[str]] = {}
        for session in sessions:
            identity = AuthenticatedReplica(session.principal_id, session.replica_id)
            if identity in self._grants:
                raise ValueError("principal session identities must be unique")
            self._grants[identity] = session.grants
            self._capabilities[identity] = session.capabilities
        self._principal_capabilities: dict[str, frozenset[str]] = {}
        for account in accounts:
            if account.principal_id in self._principal_capabilities:
                raise ValueError("principal accounts must be unique")
            self._principal_capabilities[account.principal_id] = account.capabilities

    def access(self, identity: AuthenticatedReplica, log_id: str) -> str | None:
        grants = self._grants.get(identity, {})
        capabilities = self._capabilities.get(identity)
        if capabilities is None:
            capabilities = self._principal_capabilities.get(identity.principal_id)
        if capabilities is None or identity.replica_id not in self._directory.replicas(identity.principal_id):
            return None
        explicit = grants.get(log_id)
        if explicit is not None:
            return explicit
        if "playgrounds" in capabilities and log_id == "playground-releases":
            return "read"
        parts = log_id.split("/")
        principal, replica = _encoded_identity(identity)
        if (
            "drafts" in capabilities
            and len(parts) >= 5
            and parts[0] == "principal"
            and parts[1] == principal
            and parts[2] == "draft"
            and parts[3]
        ):
            if len(parts) == 5 and parts[4] in ("decision", "comment"):
                return "read"
            if len(parts) == 6 and parts[5]:
                if parts[4] == "request":
                    return "append" if parts[5] == replica else None
                if parts[4] == "revision":
                    if parts[5] == replica:
                        return "append"
                    known = {
                        quote(value, safe="-_.!~*'()")
                        for value in self._directory.replicas(identity.principal_id)
                    }
                    return "read" if parts[5] in known else None
        if (
            "submissions" in capabilities
            and len(parts) == 5
            and parts[0] == "principal"
            and parts[1] == principal
            and parts[2] == "submission"
            and parts[3] == replica
        ):
            if parts[4] == "requests":
                return "append"
            if parts[4] == "results":
                return "read"
        return None

    def has_access(self, identity: AuthenticatedReplica) -> bool:
        if identity.replica_id not in self._directory.replicas(identity.principal_id):
            return False
        return bool(
            self._grants.get(identity)
            or self._capabilities.get(identity)
            or self._principal_capabilities.get(identity.principal_id)
        )


def permits_log(access: str | None, action: str, method: str) -> bool:
    if access is None:
        return False
    if method == "POST":
        return action == "records" and access == "append"
    if method in ("GET", "HEAD") and action in ("head", "has", "records"):
        return access == "read" or action in ("head", "has")
    return False


def _encoded_identity(identity: AuthenticatedReplica) -> tuple[str, str]:
    safe = "-_.!~*'()"
    return quote(identity.principal_id, safe=safe), quote(identity.replica_id, safe=safe)


def _replica_owner(log_id: str) -> tuple[str, str] | None:
    parts = log_id.split("/")
    if (
        len(parts) == 6
        and parts[0] == "principal"
        and parts[2] == "draft"
        and parts[4] in ("revision", "request")
    ):
        return parts[1], parts[5]
    if (
        len(parts) == 5
        and parts[0] == "principal"
        and parts[2] == "submission"
        and parts[4] == "requests"
    ):
        return parts[1], parts[3]
    return None
