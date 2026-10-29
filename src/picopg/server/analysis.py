"""Authorization and limits for student-owned learning-event logs."""

import json

from picopg.replica import ReplicatedRecord

from picopg.server.common.ids import decode_component, encode_component


MAX_EVIDENCE_BYTES = 64 * 1024


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
        or parts[2] != "analysis"
        or parts[4] != "evidence"
    ):
        raise ValueError("Log is not a Learning Analysis Evidence Log")
    return (
        decode_component(parts[1], "principalId"),
        decode_component(parts[3], "replicaId"),
    )


def validate_evidence_size(
    log_id: str, records: tuple[ReplicatedRecord, ...]
) -> None:
    try:
        evidence_scope(log_id)
    except ValueError:
        return
    for record in records:
        encoded = json.dumps(
            record.to_value()["data"],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_EVIDENCE_BYTES:
            raise ValueError(
                f"Evidence exceeds {MAX_EVIDENCE_BYTES} bytes"
            )
