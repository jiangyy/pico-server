"""Server-owned Draft decisions over replica-owned Revision and Request Logs."""

from __future__ import annotations

from collections.abc import Mapping

from picopg.api import Position, ReplicaLogStore, ReplicatedRecord, record_id_of

from apps.server.draft.protocol import (
    DraftRef,
    DraftResult,
    decision_log_id,
    decode_revision,
    draft_scope,
    position,
    project_draft,
    replayed_command,
    request_log_id,
    revision_log_id,
    text,
)

PAGE_SIZE = 128


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


class DraftAuthority:
    """Consumes one request Position and conditionally appends a Draft decision."""

    def __init__(self, store: ReplicaLogStore) -> None:
        self._store = store

    def initialize(self, principal_id: str, draft_id: str, initial_head: Position) -> DraftResult:
        try:
            principal_id = text(principal_id, "principalId")
            draft_id = text(draft_id, "draftId")
            revision = _at(self._store, initial_head)
            if revision is None:
                raise ValueError("initial Revision is not available")
            decoded = decode_revision(revision)
            if decoded["principalId"] != principal_id or decoded["draftId"] != draft_id:
                raise ValueError("initial Revision belongs to another Principal or Draft")
            if decoded["base"] is not None:
                raise ValueError("initial Revision must have a null base")
            existing = _records(self._store, decision_log_id(principal_id, draft_id))
            if existing:
                ref = project_draft(existing)
                if ref.head != initial_head:
                    raise ValueError("Draft was initialized with another Revision")
                return DraftResult("current", ref, existing[-1].position)
            data = {"type": "draft.started", "draftId": draft_id, "head": initial_head.to_value()}
            record = _record(decision_log_id(principal_id, draft_id), None, (), data)
            result = self._store.append(record.log_id, None, (record,))
            if result.kind != "accepted":
                return self.initialize(principal_id, draft_id, initial_head)
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
            principal_id, request_draft_id, request_replica_id = draft_scope(
                request.log_id, "request"
            )
            draft_id = text(command.get("draftId"), "command.draftId")
            replica_id = text(command.get("replicaId"), "command.replicaId")
            command_id = text(command.get("commandId"), "command.commandId")
            if draft_id != request_draft_id or replica_id != request_replica_id:
                raise ValueError("command is stored in the wrong Request Log")
            if request.log_id != request_log_id(principal_id, draft_id, replica_id):
                raise ValueError("command is stored in the wrong Request Log")
            decision_log = decision_log_id(principal_id, draft_id)
            required = [position(command.get("expectedHead"), "command.expectedHead")]
            if kind in ("draft.takeover", "draft.advance"):
                required.append(position(command.get("targetHead"), "command.targetHead"))
            elif kind != "draft.beginEditing":
                raise ValueError("command type is invalid")
            for required_position in required:
                if required_position not in request.requires:
                    raise ValueError("command does not declare all referenced Revisions in requires")
            while True:
                decisions = _records(self._store, decision_log)
                if not decisions:
                    if kind != "draft.beginEditing":
                        raise ValueError("Draft is not initialized")
                    initialized = self.initialize(principal_id, draft_id, required[0])
                    if initialized.kind not in ("accepted", "current"):
                        raise ValueError(initialized.message or "Draft could not be initialized")
                    continue
                ref = project_draft(decisions)
                revision_match = replayed_command(decisions, command_id)
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
                    revision = decode_revision(target)
                    if revision["principalId"] != principal_id or revision["draftId"] != draft_id:
                        raise ValueError("Revision belongs to another Principal or Draft")
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
            "draftId": text(first.get("draftId"), "decision.draftId"),
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
