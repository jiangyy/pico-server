"""Durable browser Sessions and Principal-local Replica registration."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from uuid import uuid4

from apps.server.authorization import AuthenticatedReplica


SESSION_TTL_SECONDS = 30 * 24 * 60 * 60


class SessionStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrowserSession:
    identity: AuthenticatedReplica
    session_token: str
    device_token: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class OperatorSession:
    token: str
    expires_at: int


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _non_empty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty")
    return value


class FileSessionRegistry:
    """One-process registry; tokens are persisted only as SHA-256 digests."""

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], float] = time.time,
        token: Callable[[int], str] = secrets.token_urlsafe,
        ttl_seconds: int = SESSION_TTL_SECONDS,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self._path = path
        self._now = now
        self._token = token
        self._ttl = ttl_seconds
        self._lock = RLock()
        self._replicas: dict[tuple[str, str], str] = {}
        self._sessions: dict[str, tuple[AuthenticatedReplica, int]] = {}
        self._operators: dict[str, int] = {}
        self._load()

    def bootstrap(self, principal_id: str, device_token: str | None) -> BrowserSession:
        principal_id = _non_empty(principal_id, "principalId")
        raw_device = self._token(32) if device_token is None else _non_empty(device_token, "deviceToken")
        device_digest = _digest(raw_device)
        with self._lock:
            self._prune()
            key = (principal_id, device_digest)
            replica_id = self._replicas.get(key)
            if replica_id is None:
                known = self.replicas(principal_id)
                while True:
                    candidate = f"r-{self._token(12)}"
                    if candidate not in known:
                        replica_id = candidate
                        break
                self._replicas[key] = replica_id
            session_token = self._new_token(self._sessions)
            expires_at = int(self._now()) + self._ttl
            identity = AuthenticatedReplica(principal_id, replica_id)
            self._sessions[_digest(session_token)] = (identity, expires_at)
            self._write()
            return BrowserSession(identity, session_token, raw_device, expires_at)

    def create_operator(self) -> OperatorSession:
        with self._lock:
            self._prune()
            token = self._new_token(self._operators)
            expires_at = int(self._now()) + self._ttl
            self._operators[_digest(token)] = expires_at
            self._write()
            return OperatorSession(token, expires_at)

    def authenticate(self, token: str) -> AuthenticatedReplica | None:
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            entry = self._sessions.get(_digest(token))
            if entry is None or entry[1] <= int(self._now()):
                return None
            return entry[0]

    def authenticate_operator(self, token: str) -> bool:
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            expires_at = self._operators.get(_digest(token))
            return expires_at is not None and expires_at > int(self._now())

    def revoke(self, token: str) -> bool:
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            removed = self._sessions.pop(_digest(token), None) is not None
            if removed:
                self._write()
            return removed

    def revoke_operator(self, token: str) -> bool:
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            removed = self._operators.pop(_digest(token), None) is not None
            if removed:
                self._write()
            return removed

    def replicas(self, principal_id: str) -> frozenset[str]:
        with self._lock:
            return frozenset(
                replica_id
                for (owner, _device), replica_id in self._replicas.items()
                if owner == principal_id
            )

    def _new_token(self, entries: Mapping[str, object]) -> str:
        while True:
            token = self._token(32)
            if _digest(token) not in entries:
                return token

    def _prune(self) -> None:
        now = int(self._now())
        self._sessions = {
            digest: entry for digest, entry in self._sessions.items()
            if entry[1] > now
        }
        self._operators = {
            digest: expires_at for digest, expires_at in self._operators.items()
            if expires_at > now
        }

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            raise SessionStoreError(f"cannot open {self._path}") from error
        try:
            if not isinstance(raw, dict) or set(raw) != {"schema", "replicas", "sessions", "operators"}:
                raise ValueError("session document shape is invalid")
            if raw["schema"] != 1:
                raise ValueError("session document schema is invalid")
            replicas: dict[tuple[str, str], str] = {}
            identities: set[tuple[str, str]] = set()
            for item in raw["replicas"]:
                if not isinstance(item, dict) or set(item) != {"principalId", "deviceDigest", "replicaId"}:
                    raise ValueError("replica registration is invalid")
                principal_id = _non_empty(item["principalId"], "principalId")
                device_digest = _non_empty(item["deviceDigest"], "deviceDigest")
                replica_id = _non_empty(item["replicaId"], "replicaId")
                if len(device_digest) != 64:
                    raise ValueError("deviceDigest is invalid")
                key = (principal_id, device_digest)
                identity = (principal_id, replica_id)
                if key in replicas or identity in identities:
                    raise ValueError("replica registration is duplicated")
                replicas[key] = replica_id
                identities.add(identity)
            sessions: dict[str, tuple[AuthenticatedReplica, int]] = {}
            for item in raw["sessions"]:
                if not isinstance(item, dict) or set(item) != {"digest", "principalId", "replicaId", "expiresAt"}:
                    raise ValueError("session is invalid")
                digest = _non_empty(item["digest"], "digest")
                identity = AuthenticatedReplica(
                    _non_empty(item["principalId"], "principalId"),
                    _non_empty(item["replicaId"], "replicaId"),
                )
                expires_at = item["expiresAt"]
                if len(digest) != 64 or type(expires_at) is not int:
                    raise ValueError("session is invalid")
                if identity.replica_id not in {
                    replica for (owner, _device), replica in replicas.items()
                    if owner == identity.principal_id
                }:
                    raise ValueError("session references an unknown Replica")
                if digest in sessions:
                    raise ValueError("session is duplicated")
                sessions[digest] = (identity, expires_at)
            operators: dict[str, int] = {}
            for item in raw["operators"]:
                if not isinstance(item, dict) or set(item) != {"digest", "expiresAt"}:
                    raise ValueError("operator session is invalid")
                digest = _non_empty(item["digest"], "digest")
                expires_at = item["expiresAt"]
                if len(digest) != 64 or type(expires_at) is not int or digest in operators:
                    raise ValueError("operator session is invalid")
                operators[digest] = expires_at
        except (KeyError, TypeError, ValueError) as error:
            raise SessionStoreError(f"cannot open {self._path}") from error
        self._replicas = replicas
        self._sessions = sessions
        self._operators = operators
        self._prune()

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.parent / f".{self._path.name}-{uuid4().hex}.tmp"
        document = {
            "schema": 1,
            "replicas": [
                {"principalId": principal, "deviceDigest": device, "replicaId": replica}
                for (principal, device), replica in sorted(self._replicas.items())
            ],
            "sessions": [
                {
                    "digest": digest,
                    "principalId": identity.principal_id,
                    "replicaId": identity.replica_id,
                    "expiresAt": expires_at,
                }
                for digest, (identity, expires_at) in sorted(self._sessions.items())
            ],
            "operators": [
                {"digest": digest, "expiresAt": expires_at}
                for digest, expires_at in sorted(self._operators.items())
            ],
        }
        payload = (json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
        except OSError as error:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise SessionStoreError(f"cannot persist {self._path}") from error
