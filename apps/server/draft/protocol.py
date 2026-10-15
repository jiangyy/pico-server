from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from apps.server.log_ids import decode_component, encode_component
from apps.server.validation import require_position as position
from apps.server.validation import require_text as text
from picopg.api import Position, ReplicatedRecord


class DraftLogRule:
    capability = "drafts"

    def access(
        self,
        principal_id: str,
        replica_id: str,
        replicas: frozenset[str],
        log_id: str,
    ) -> str | None:
        parts = log_id.split("/")
        principal = encode_component(principal_id, "principalId")
        replica = encode_component(replica_id, "replicaId")
        if (
            len(parts) < 5
            or parts[0] != "principal"
            or parts[1] != principal
            or parts[2] != "draft"
            or not parts[3]
        ):
            return None
        if len(parts) == 5 and parts[4] in ("decision", "comment"):
            return "read"
        if len(parts) != 6 or not parts[5]:
            return None
        if parts[4] == "request":
            return "append" if parts[5] == replica else None
        if parts[4] != "revision":
            return None
        if parts[5] == replica:
            return "append"
        known = {encode_component(value, "replicaId") for value in replicas}
        return "read" if parts[5] in known else None

    def owner(self, log_id: str) -> tuple[str, str] | None:
        parts = log_id.split("/")
        if (
            len(parts) == 6
            and parts[0] == "principal"
            and parts[2] == "draft"
            and parts[4] in ("revision", "request")
        ):
            return parts[1], parts[5]
        return None


def revision_log_id(principal_id: str, draft_id: str, replica_id: str) -> str:
    return (
        f"principal/{encode_component(principal_id, 'principalId')}"
        f"/draft/{encode_component(draft_id, 'draftId')}"
        f"/revision/{encode_component(replica_id, 'replicaId')}"
    )


def request_log_id(principal_id: str, draft_id: str, replica_id: str) -> str:
    return (
        f"principal/{encode_component(principal_id, 'principalId')}"
        f"/draft/{encode_component(draft_id, 'draftId')}"
        f"/request/{encode_component(replica_id, 'replicaId')}"
    )


def decision_log_id(principal_id: str, draft_id: str) -> str:
    return (
        f"principal/{encode_component(principal_id, 'principalId')}"
        f"/draft/{encode_component(draft_id, 'draftId')}/decision"
    )


def draft_scope(log_id: str, kind: str) -> tuple[str, str, str | None]:
    parts = log_id.split("/")
    expected = 5 if kind == "decision" else 6
    if (
        len(parts) != expected
        or parts[0] != "principal"
        or not parts[1]
        or parts[2] != "draft"
        or not parts[3]
        or parts[4] != kind
        or (expected == 6 and not parts[5])
    ):
        raise ValueError(f"Log is not a Draft {kind} Log")
    return (
        decode_component(parts[1], "principalId"),
        decode_component(parts[3], "draftId"),
        None if expected == 5 else decode_component(parts[5], "replicaId"),
    )


@dataclass(frozen=True, slots=True)
class DraftRef:
    head: Position
    writer: str | None
    epoch: int


@dataclass(frozen=True, slots=True)
class DraftResult:
    kind: str
    ref: DraftRef | None
    decision: Position | None = None
    message: str | None = None


def project_draft(records: tuple[ReplicatedRecord, ...]) -> DraftRef:
    if not records:
        raise ValueError("decision Log is empty")
    first = records[0].data
    if not isinstance(first, Mapping) or first.get("type") != "draft.started":
        raise ValueError("decision Log must start with draft.started")
    principal_id, scoped_draft_id, _ = draft_scope(records[0].log_id, "decision")
    draft_id = text(first.get("draftId"), "decision.draftId")
    if draft_id != scoped_draft_id:
        raise ValueError("decision belongs to another Draft")
    log_id = decision_log_id(principal_id, draft_id)
    head = position(first.get("head"), "decision.head")
    ref = DraftRef(head, None, 0)
    commands: set[str] = set()
    parent: Position | None = None
    for index, record in enumerate(records):
        if record.log_id != log_id or record.parent != parent or record.requires:
            raise ValueError("decision Log chain is invalid")
        data = record.data
        if not isinstance(data, Mapping) or data.get("draftId") != draft_id:
            raise ValueError("decision belongs to another Draft")
        if index:
            kind = data.get("type")
            command_id = text(data.get("commandId"), "decision.commandId")
            if command_id in commands:
                raise ValueError("commandId appears more than once")
            commands.add(command_id)
            epoch = data.get("epoch")
            if type(epoch) is not int or epoch < 0:
                raise ValueError("decision.epoch must be non-negative")
            if kind == "draft.rejected":
                text(data.get("reason"), "decision.reason")
                head = position(data.get("head"), "decision.head")
                writer_value = data.get("writer")
                writer = None if writer_value is None else text(writer_value, "decision.writer")
                if head != ref.head or writer != ref.writer or epoch != ref.epoch:
                    raise ValueError("rejection does not describe the current DraftRef")
            elif kind == "draft.editingStarted":
                writer = text(data.get("writer"), "decision.writer")
                if ref.writer is not None or epoch != ref.epoch + 1:
                    raise ValueError("editing transition is invalid")
                ref = DraftRef(ref.head, writer, epoch)
            elif kind in ("draft.takenOver", "draft.advanced"):
                writer = text(data.get("writer"), "decision.writer")
                new_head = position(data.get("head"), "decision.head")
                if kind == "draft.takenOver":
                    if epoch != ref.epoch + 1:
                        raise ValueError("takeover epoch is invalid")
                elif writer != ref.writer or epoch != ref.epoch:
                    raise ValueError("advance fence is invalid")
                ref = DraftRef(new_head, writer, epoch)
            else:
                raise ValueError("decision type is invalid")
        parent = record.position
    return ref


def decode_revision(record: ReplicatedRecord) -> dict[str, object]:
    data = record.data
    if not isinstance(data, Mapping) or data.get("type") != "draft.revised":
        raise ValueError("record is not a Draft Revision")
    draft_id = text(data.get("draftId"), "revision.draftId")
    replica_id = text(data.get("replicaId"), "revision.replicaId")
    principal_id, scoped_draft_id, scoped_replica_id = draft_scope(record.log_id, "revision")
    if draft_id != scoped_draft_id or replica_id != scoped_replica_id:
        raise ValueError("Revision is stored in the wrong Log")
    if record.log_id != revision_log_id(principal_id, draft_id, replica_id):
        raise ValueError("Revision is stored in the wrong Log")
    source = data.get("source")
    if not isinstance(source, str):
        raise ValueError("revision.source must be a string")
    base = None if data.get("base") is None else position(data.get("base"), "revision.base")
    return {
        "principalId": principal_id,
        "draftId": draft_id,
        "replicaId": replica_id,
        "source": source,
        "base": base,
    }


def replayed_command(
    decisions: tuple[ReplicatedRecord, ...], command_id: str
) -> tuple[Position, Position, DraftRef, str] | None:
    for index, decision in enumerate(decisions[1:], start=1):
        data = decision.data
        if isinstance(data, Mapping) and data.get("commandId") == command_id:
            return (
                position(data.get("request"), "decision.request"),
                decision.position,
                project_draft(decisions[: index + 1]),
                text(data.get("reason"), "decision.reason")
                if data.get("type") == "draft.rejected" else "current",
            )
    return None
