from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote, unquote


class SubmissionLogRule:
    capability = "submissions"

    def access(
        self,
        principal_id: str,
        replica_id: str,
        replicas: frozenset[str],
        log_id: str,
    ) -> str | None:
        parts = log_id.split("/")
        if (
            len(parts) != 5
            or parts[0] != "principal"
            or parts[1] != quote(principal_id, safe="-_.!~*'()")
            or parts[2] != "submission"
            or parts[3] != quote(replica_id, safe="-_.!~*'()")
        ):
            return None
        if parts[4] == "requests":
            return "append"
        if parts[4] == "results":
            return "read"
        return None

    def owner(self, log_id: str) -> tuple[str, str] | None:
        parts = log_id.split("/")
        if (
            len(parts) == 5
            and parts[0] == "principal"
            and parts[2] == "submission"
            and parts[4] == "requests"
        ):
            return parts[1], parts[3]
        return None


def request_logs(bindings: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(sorted(
        log_id for log_id, access in bindings.items()
        if access == "append" and is_request_log(log_id)
    ))


def result_log(request_log: str) -> str:
    principal, replica = request_scope(request_log)
    safe = "-_.!~*'()"
    return f"principal/{quote(principal, safe=safe)}/submission/{quote(replica, safe=safe)}/results"


def is_request_log(log_id: str) -> bool:
    parts = log_id.split("/")
    return (
        len(parts) == 5
        and parts[0] == "principal"
        and bool(parts[1])
        and parts[2] == "submission"
        and bool(parts[3])
        and parts[4] == "requests"
    )


def request_scope(log_id: str) -> tuple[str, str]:
    if not is_request_log(log_id):
        raise ValueError("request_log is not a Submission Request Log")
    parts = log_id.split("/")
    return unquote(parts[1]), unquote(parts[3])
