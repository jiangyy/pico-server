"""Authentication and principal-scoped Log authorization."""

from __future__ import annotations

import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Protocol

from apps.server.log_ids import encode_component
from apps.server.validation import require_text


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


class PrincipalLogRule(Protocol):
    capability: str

    def access(
        self,
        principal_id: str,
        replica_id: str,
        replicas: frozenset[str],
        log_id: str,
    ) -> str | None: ...

    def owner(self, log_id: str) -> tuple[str, str] | None: ...


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
        require_text(principal_id, "principalId")
        require_text(access_code, "accessCode")
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
        require_text(principal_id, "principalId")
        require_text(replica_id, "replicaId")
        require_text(token, "token")
        copied = dict(grants)
        if any(not isinstance(log_id, str) or not log_id for log_id in copied):
            raise ValueError("grant Log IDs must be non-empty")
        if any(access not in ("read", "append") for access in copied.values()):
            raise ValueError("grant access must be 'read' or 'append'")
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
        rules: Iterable[PrincipalLogRule] = (),
    ) -> None:
        sessions = tuple(sessions)
        accounts = tuple(accounts)
        self._directory = directory
        self._rules = tuple(rules)
        configured = {rule.capability for rule in self._rules}
        if len(configured) != len(self._rules):
            raise ValueError("principal Log rule capabilities must be unique")
        unknown = configured - PRINCIPAL_CAPABILITIES
        if unknown:
            raise ValueError(
                f"unknown principal Log rule capabilities: {', '.join(sorted(unknown))}"
            )
        enabled = {
            capability
            for principal in (*sessions, *accounts)
            for capability in principal.capabilities
        }
        missing = enabled - configured
        if missing:
            raise ValueError(
                f"principal capabilities have no Log rules: {', '.join(sorted(missing))}"
            )
        self._grants: dict[AuthenticatedReplica, Mapping[str, str]] = {}
        self._capabilities: dict[AuthenticatedReplica, frozenset[str]] = {}
        for session in sessions:
            identity = AuthenticatedReplica(session.principal_id, session.replica_id)
            if identity in self._grants:
                raise ValueError("principal session identities must be unique")
            self._validate_grants(identity, session.grants)
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
        replicas = self._directory.replicas(identity.principal_id)
        if capabilities is None or identity.replica_id not in replicas:
            return None
        explicit = grants.get(log_id)
        if explicit is not None:
            return explicit
        for rule in self._rules:
            if rule.capability not in capabilities:
                continue
            access = rule.access(
                identity.principal_id,
                identity.replica_id,
                replicas,
                log_id,
            )
            if access is not None:
                return access
        return None

    def has_access(self, identity: AuthenticatedReplica) -> bool:
        if identity.replica_id not in self._directory.replicas(identity.principal_id):
            return False
        return bool(
            self._grants.get(identity)
            or self._capabilities.get(identity)
            or self._principal_capabilities.get(identity.principal_id)
        )

    def _validate_grants(
        self,
        identity: AuthenticatedReplica,
        grants: Mapping[str, str],
    ) -> None:
        encoded_identity = _encoded_identity(identity)
        for log_id, access in grants.items():
            if access != "append":
                continue
            owner = next(
                (
                    owner
                    for rule in self._rules
                    if (owner := rule.owner(log_id)) is not None
                ),
                None,
            )
            if owner is not None and owner != encoded_identity:
                raise ValueError(f"append grant {log_id!r} belongs to another Replica")


def permits_log(access: str | None, action: str, method: str) -> bool:
    if access is None:
        return False
    if method == "POST":
        return action == "records" and access == "append"
    if method in ("GET", "HEAD") and action in ("head", "has", "records"):
        return access == "read" or action in ("head", "has")
    return False


def _encoded_identity(identity: AuthenticatedReplica) -> tuple[str, str]:
    return (
        encode_component(identity.principal_id, "principalId"),
        encode_component(identity.replica_id, "replicaId"),
    )
