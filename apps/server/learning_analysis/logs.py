from __future__ import annotations

from collections.abc import Mapping

from apps.server.log_ids import decode_component, encode_component


class LearningAnalysisLogRule:
    capability = "analysis"

    def access(
        self,
        principal_id: str,
        replica_id: str,
        replicas: frozenset[str],
        log_id: str,
    ) -> str | None:
        parts = log_id.split("/")
        if (
            len(parts) < 4
            or parts[0] != "principal"
            or parts[1] != encode_component(principal_id, "principalId")
            or parts[2] != "analysis"
        ):
            return None
        if len(parts) == 4 and parts[3] == "results":
            return "read"
        if (
            len(parts) == 5
            and parts[3] == encode_component(replica_id, "replicaId")
            and parts[4] == "evidence"
        ):
            return "append"
        return None

    def owner(self, log_id: str) -> tuple[str, str] | None:
        parts = log_id.split("/")
        if (
            len(parts) == 5
            and parts[0] == "principal"
            and parts[2] == "analysis"
            and parts[4] == "evidence"
        ):
            return parts[1], parts[3]
        return None


def evidence_log_id(principal_id: str, replica_id: str) -> str:
    return (
        f"principal/{encode_component(principal_id, 'principalId')}"
        f"/analysis/{encode_component(replica_id, 'replicaId')}/evidence"
    )


def result_log_id(principal_id: str) -> str:
    return f"principal/{encode_component(principal_id, 'principalId')}/analysis/results"


def job_log_id(principal_id: str) -> str:
    return f"server/analysis/{encode_component(principal_id, 'principalId')}/jobs"


def is_evidence_log(log_id: str) -> bool:
    parts = log_id.split("/")
    return (
        len(parts) == 5
        and parts[0] == "principal"
        and bool(parts[1])
        and parts[2] == "analysis"
        and bool(parts[3])
        and parts[4] == "evidence"
    )


def evidence_logs(bindings: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            log_id
            for log_id, access in bindings.items()
            if access == "append" and is_evidence_log(log_id)
        )
    )


def evidence_scope(log_id: str) -> tuple[str, str]:
    if not is_evidence_log(log_id):
        raise ValueError("Log is not a Learning Analysis Evidence Log")
    parts = log_id.split("/")
    return (
        decode_component(parts[1], "principalId"),
        decode_component(parts[3], "replicaId"),
    )
