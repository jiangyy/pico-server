from __future__ import annotations

from collections.abc import Mapping

from apps.server.log_ids import decode_component, encode_component


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
            or parts[1] != encode_component(principal_id, "principalId")
            or parts[2] != "submission"
            or parts[3] != encode_component(replica_id, "replicaId")
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
    return (
        f"principal/{encode_component(principal, 'principalId')}"
        f"/submission/{encode_component(replica, 'replicaId')}/results"
    )


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
    return (
        decode_component(parts[1], "principalId"),
        decode_component(parts[3], "replicaId"),
    )
