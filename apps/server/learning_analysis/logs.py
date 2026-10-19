from __future__ import annotations

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


def evidence_scope(log_id: str) -> tuple[str, str]:
    parts = log_id.split("/")
    if (
        len(parts) != 5
        or parts[0] != "principal"
        or not parts[1]
        or parts[2] != "analysis"
        or not parts[3]
        or parts[4] != "evidence"
    ):
        raise ValueError("Log is not a Learning Analysis Evidence Log")
    return (
        decode_component(parts[1], "principalId"),
        decode_component(parts[3], "replicaId"),
    )
