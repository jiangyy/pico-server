"""Server-owned Draft decisions over replica-owned Revision and Request Logs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

from picopg.api import Position, ReplicaLogStore, ReplicatedRecord, record_id_of

PAGE_SIZE = 128


def revision_log_id(draft_id: str, replica_id: str) -> str:
    return f"draft/{_component(draft_id, 'draftId')}/revision/{_component(replica_id, 'replicaId')}"


def request_log_id(draft_id: str, replica_id: str) -> str:
    return f"draft/{_component(draft_id, 'draftId')}/request/{_component(replica_id, 'replicaId')}"


def decision_log_id(draft_id: str) -> str:
    return f"draft/{_component(draft_id, 'draftId')}/decision"


def _component(value: str, label: str) -> str:
    return quote(_text(value, label), safe="-_.!~*'()")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty")
    return value


def _position(value: object, label: str = "position") -> Position:
    if not isinstance(value, Mapping) or set(value) != {"logId", "recordId"}:
        raise ValueError(f"{label} must contain logId and recordId")
    return Position(_text(value["logId"], f"{label}.logId"), _text(value["recordId"], f"{label}.recordId"))


def _same(left: Position | None, right: Position | None) -> bool:
    return left == right


def _records(store: ReplicaLogStore, log_id: str) -> tuple[ReplicatedRecord, ...]:
    result: list[ReplicatedRecord] = []
    after: Position | None = None
    while True:
        page = store.read(log_id, after, PAGE_SIZE)
        result.extend(page.records)
        if not page.has_more:
            return tuple(result)
        if not page.records:
            raise ValueError(f"{log_id} returned an empty continuation page")
        after = page.records[-1].position


def _at(store: ReplicaLogStore, position: Position) -> ReplicatedRecord | None:
    if not store.has(position):
        return None
    return next((record for record in _records(store, position.log_id) if record.id == position.record_id), None)


def _record(
    log_id: str,
    parent: Position | None,
    requires: tuple[Position, ...],
    data: Mapping[str, object],
) -> ReplicatedRecord:
    return ReplicatedRecord(
        record_id_of(log_id, parent, requires, data), log_id, parent, requires, data
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
    draft_id = _text(first.get("draftId"), "decision.draftId")
    log_id = decision_log_id(draft_id)
    head = _position(first.get("head"), "decision.head")
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
            command_id = _text(data.get("commandId"), "decision.commandId")
            if command_id in commands:
                raise ValueError("commandId appears more than once")
            commands.add(command_id)
            epoch = data.get("epoch")
            if type(epoch) is not int or epoch < 0:
                raise ValueError("decision.epoch must be non-negative")
            if kind == "draft.rejected":
                _text(data.get("reason"), "decision.reason")
                head = _position(data.get("head"), "decision.head")
                writer_value = data.get("writer")
                writer = None if writer_value is None else _text(writer_value, "decision.writer")
                if head != ref.head or writer != ref.writer or epoch != ref.epoch:
                    raise ValueError("rejection does not describe the current DraftRef")
            elif kind == "draft.editingStarted":
                writer = _text(data.get("writer"), "decision.writer")
                if ref.writer is not None or epoch != ref.epoch + 1:
                    raise ValueError("editing transition is invalid")
                ref = DraftRef(ref.head, writer, epoch)
            elif kind in ("draft.takenOver", "draft.advanced"):
                writer = _text(data.get("writer"), "decision.writer")
                new_head = _position(data.get("head"), "decision.head")
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


class DraftAuthority:
    """Consumes one request Position and conditionally appends a Draft decision."""

    def __init__(self, store: ReplicaLogStore) -> None:
        self._store = store

    def initialize(self, draft_id: str, initial_head: Position) -> DraftResult:
        try:
            draft_id = _text(draft_id, "draftId")
            revision = _at(self._store, initial_head)
            if revision is None:
                raise ValueError("initial Revision is not available")
            if _decode_revision(revision)["base"] is not None:
                raise ValueError("initial Revision must have a null base")
            existing = _records(self._store, decision_log_id(draft_id))
            if existing:
                ref = project_draft(existing)
                if ref.head != initial_head:
                    raise ValueError("Draft was initialized with another Revision")
                return DraftResult("current", ref, existing[-1].position)
            data = {"type": "draft.started", "draftId": draft_id, "head": initial_head.to_value()}
            record = _record(decision_log_id(draft_id), None, (), data)
            result = self._store.append(record.log_id, None, (record,))
            if result.kind != "accepted":
                return self.initialize(draft_id, initial_head)
            return DraftResult("accepted", DraftRef(initial_head, None, 0), record.position)
        except (TypeError, ValueError) as error:
            return DraftResult("invalid", None, message=str(error))

    def handle(self, request_position: Position) -> DraftResult:
        ref: DraftRef | None = None
        try:
            request = _at(self._store, request_position)
            if request is None:
                return DraftResult("missingDependency", None, message="request is not available")
            command = request.data
            if not isinstance(command, Mapping):
                raise ValueError("command must be an object")
            kind = command.get("type")
            draft_id = _text(command.get("draftId"), "command.draftId")
            replica_id = _text(command.get("replicaId"), "command.replicaId")
            command_id = _text(command.get("commandId"), "command.commandId")
            if request.log_id != request_log_id(draft_id, replica_id):
                raise ValueError("command is stored in the wrong Request Log")
            decision_log = decision_log_id(draft_id)
            required = [_position(command.get("expectedHead"), "command.expectedHead")]
            if kind in ("draft.takeover", "draft.advance"):
                required.append(_position(command.get("targetHead"), "command.targetHead"))
            elif kind != "draft.beginEditing":
                raise ValueError("command type is invalid")
            for position in required:
                if position not in request.requires:
                    raise ValueError("command does not declare all referenced Revisions in requires")
            while True:
                decisions = _records(self._store, decision_log)
                if not decisions:
                    if kind != "draft.beginEditing":
                        raise ValueError("Draft is not initialized")
                    initialized = self.initialize(draft_id, required[0])
                    if initialized.kind not in ("accepted", "current"):
                        raise ValueError(initialized.message or "Draft could not be initialized")
                    continue
                ref = project_draft(decisions)
                revision_match = _replayed_command(decisions, command_id)
                if revision_match is not None:
                    prior_request, prior_decision, prior_ref, prior_kind = revision_match
                    if prior_request != request_position:
                        raise ValueError("commandId was reused by another request")
                    return DraftResult(prior_kind, prior_ref, prior_decision)
                expected_epoch = command.get("expectedEpoch")
                if type(expected_epoch) is not int or expected_epoch < 0:
                    rejected = self._reject(decisions, request_position, command_id, "invalid", ref)
                    if rejected is None:
                        continue
                    return DraftResult("invalid", ref, rejected, "command.expectedEpoch must be non-negative")
                if kind == "draft.beginEditing":
                    if ref.writer is not None and ref.writer != replica_id:
                        rejected = self._reject(decisions, request_position, command_id, "activeElsewhere", ref)
                        if rejected is None:
                            continue
                        return DraftResult("activeElsewhere", ref, rejected)
                    if required[0] != ref.head or expected_epoch != ref.epoch:
                        rejected = self._reject(decisions, request_position, command_id, "changed", ref)
                        if rejected is None:
                            continue
                        return DraftResult("changed", ref, rejected)
                    if ref.writer == replica_id:
                        return DraftResult("current", ref)
                    data = {"type": "draft.editingStarted", "draftId": draft_id, "commandId": command_id,
                            "request": request_position.to_value(), "writer": replica_id, "epoch": ref.epoch + 1}
                else:
                    target = _at(self._store, required[1])
                    if target is None:
                        return DraftResult("missingDependency", ref, message="referenced Revision is not available")
                    revision = _decode_revision(target)
                    if revision["draftId"] != draft_id:
                        raise ValueError("Revision belongs to another Draft")
                    if required[0] != ref.head or expected_epoch != ref.epoch:
                        rejected = self._reject(decisions, request_position, command_id, "changed", ref)
                        if rejected is None:
                            continue
                        return DraftResult("changed", ref, rejected)
                    if kind == "draft.takeover":
                        if revision["replicaId"] != replica_id:
                            rejected = self._reject(decisions, request_position, command_id, "invalid", ref)
                            if rejected is None:
                                continue
                            return DraftResult("invalid", ref, rejected, "takeover Revision belongs to another Replica")
                        data = {"type": "draft.takenOver", "draftId": draft_id, "commandId": command_id,
                                "request": request_position.to_value(), "head": required[1].to_value(),
                                "writer": replica_id, "epoch": ref.epoch + 1}
                    else:
                        if ref.writer != replica_id or revision["replicaId"] != replica_id:
                            rejected = self._reject(decisions, request_position, command_id, "changed", ref)
                            if rejected is None:
                                continue
                            return DraftResult("changed", ref, rejected)
                        if revision["base"] != required[0]:
                            rejected = self._reject(decisions, request_position, command_id, "invalid", ref)
                            if rejected is None:
                                continue
                            return DraftResult("invalid", ref, rejected, "Revision was not edited from expectedHead")
                        data = {"type": "draft.advanced", "draftId": draft_id, "commandId": command_id,
                                "request": request_position.to_value(), "head": required[1].to_value(),
                                "writer": replica_id, "epoch": expected_epoch}
                record = _record(decision_log, decisions[-1].position, (), data)
                appended = self._store.append(decision_log, decisions[-1].position, (record,))
                if appended.kind != "accepted":
                    continue
                return DraftResult("accepted", project_draft(_records(self._store, decision_log)), record.position)
        except (TypeError, ValueError) as error:
            return DraftResult("invalid", ref, message=str(error))

    def _reject(
        self,
        decisions: tuple[ReplicatedRecord, ...],
        request: Position,
        command_id: str,
        reason: str,
        ref: DraftRef,
    ) -> Position | None:
        first = decisions[0].data
        if not isinstance(first, Mapping):
            raise ValueError("decision Log is invalid")
        data = {
            "type": "draft.rejected",
            "draftId": _text(first.get("draftId"), "decision.draftId"),
            "commandId": command_id,
            "request": request.to_value(),
            "reason": reason,
            "head": ref.head.to_value(),
            "writer": ref.writer,
            "epoch": ref.epoch,
        }
        record = _record(decisions[-1].log_id, decisions[-1].position, (), data)
        appended = self._store.append(record.log_id, decisions[-1].position, (record,))
        return record.position if appended.kind == "accepted" else None


def _decode_revision(record: ReplicatedRecord) -> dict[str, object]:
    data = record.data
    if not isinstance(data, Mapping) or data.get("type") != "draft.revised":
        raise ValueError("record is not a Draft Revision")
    draft_id = _text(data.get("draftId"), "revision.draftId")
    replica_id = _text(data.get("replicaId"), "revision.replicaId")
    if record.log_id != revision_log_id(draft_id, replica_id):
        raise ValueError("Revision is stored in the wrong Log")
    source = data.get("source")
    if not isinstance(source, str):
        raise ValueError("revision.source must be a string")
    base = None if data.get("base") is None else _position(data.get("base"), "revision.base")
    return {"draftId": draft_id, "replicaId": replica_id, "source": source, "base": base}


def _replayed_command(
    decisions: tuple[ReplicatedRecord, ...], command_id: str
    ) -> tuple[Position, Position, DraftRef, str] | None:
    for index, decision in enumerate(decisions[1:], start=1):
        data = decision.data
        if isinstance(data, Mapping) and data.get("commandId") == command_id:
            return (
                _position(data.get("request"), "decision.request"),
                decision.position,
                project_draft(decisions[: index + 1]),
                _text(data.get("reason"), "decision.reason")
                if data.get("type") == "draft.rejected" else "current",
            )
    return None
