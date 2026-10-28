from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from picopg.server.auth.registry import FileSessionRegistry


def _tokens(*values: str):
    pending = iter(values)
    return lambda _bytes: next(pending)


class SessionsTest(unittest.TestCase):
    def test_sessions(self) -> None:
        now = [1_000.0]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sessions.json"
            registry = FileSessionRegistry(
                path,
                now=lambda: now[0],
                token=_tokens("device", "replica-a", "session-a", "session-b", "replica-b", "session-c", "operator"),
                ttl_seconds=100,
            )
            first = registry.bootstrap("user", None)
            assert first.identity.principal_id == "user"
            assert first.identity.replica_id == "r-replica-a"
            assert registry.authenticate(first.session_token) == first.identity
            assert registry.replicas("user") == frozenset(("r-replica-a",))

            same_device = registry.bootstrap("user", first.device_token)
            assert same_device.identity == first.identity
            other_principal = registry.bootstrap("other", first.device_token)
            assert other_principal.identity.principal_id == "other"
            assert other_principal.identity.replica_id == "r-replica-b"
            assert registry.replicas("other") == frozenset(("r-replica-b",))

            operator = registry.create_operator()
            assert registry.authenticate_operator(operator.token)
            assert registry.revoke_operator(operator.token)
            assert not registry.authenticate_operator(operator.token)
            assert registry.revoke(first.session_token)
            assert registry.authenticate(first.session_token) is None
            assert registry.replicas("user") == frozenset(("r-replica-a",))

            restarted = FileSessionRegistry(path, now=lambda: now[0], ttl_seconds=100)
            assert restarted.authenticate(same_device.session_token) == same_device.identity
            assert restarted.authenticate(other_principal.session_token) == other_principal.identity
            now[0] = 1_101
            assert restarted.authenticate(same_device.session_token) is None
            assert restarted.replicas("user") == frozenset(("r-replica-a",))


if __name__ == "__main__":
    unittest.main()
