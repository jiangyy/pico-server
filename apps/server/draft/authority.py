"""Server-owned Draft decisions over replica-owned Revision and Request Logs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from apps.server.log_reads import find_record, read_all
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
    text,
)


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
class _Command:
    request: Position
    kind: str
    principal_id: str
    draft_id: str
    replica_id: str
    command_id: str
    expected_head: Position
    target_head: Position | None
    expected_epoch: object


@dataclass(frozen=True, slots=True)
class _Decision:
    kind: str
    data: Mapping[str, object] | None = None
    message: str | None = None


class DraftAuthority:
    """Consumes one request Position and conditionally appends a Draft decision."""

    def __init__(self, store: ReplicaLogStore) -> None:
        self._store = store

    def initialize(self, principal_id: str, draft_id: str, initial_head: Position) -> DraftResult:
        try:
            principal_id = text(principal_id, "principalId")
            draft_id = text(draft_id, "draftId")
            revision = find_record(self._store, initial_head)
            if revision is None:
                raise ValueError("initial Revision is not available")
            decoded = decode_revision(revision)
            if decoded["principalId"] != principal_id or decoded["draftId"] != draft_id:
                raise ValueError("initial Revision belongs to another Principal or Draft")
            if decoded["base"] is not None:
                raise ValueError("initial Revision must have a null base")
            existing = read_all(self._store, decision_log_id(principal_id, draft_id))
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
        request = find_record(self._store, request_position)
        if request is None:
            return DraftResult(
                "missingDependency",
                None,
                message="request is not available",
            )
        try:
            command = self._decode(request)
        except (TypeError, ValueError) as error:
            return DraftResult("invalid", None, message=str(error))
        return self._run(command)

    def _decode(self, request: ReplicatedRecord) -> _Command:
        data = request.data
        if not isinstance(data, Mapping):
            raise ValueError("command must be an object")
        kind = data.get("type")
        principal_id, request_draft_id, request_replica_id = draft_scope(
            request.log_id, "request"
        )
        draft_id = text(data.get("draftId"), "command.draftId")
        replica_id = text(data.get("replicaId"), "command.replicaId")
        command_id = text(data.get("commandId"), "command.commandId")
        if draft_id != request_draft_id or replica_id != request_replica_id:
            raise ValueError("command is stored in the wrong Request Log")
        if request.log_id != request_log_id(principal_id, draft_id, replica_id):
            raise ValueError("command is stored in the wrong Request Log")
        expected_head = position(data.get("expectedHead"), "command.expectedHead")
        target_head = None
        if kind in ("draft.takeover", "draft.advance"):
            target_head = position(data.get("targetHead"), "command.targetHead")
        elif kind != "draft.beginEditing":
            raise ValueError("command type is invalid")
        required = (expected_head,) if target_head is None else (expected_head, target_head)
        if any(item not in request.requires for item in required):
            raise ValueError(
                "command does not declare all referenced Revisions in requires"
            )
        return _Command(
            request.position,
            kind,
            principal_id,
            draft_id,
            replica_id,
            command_id,
            expected_head,
            target_head,
            data.get("expectedEpoch"),
        )

    def _run(self, command: _Command) -> DraftResult:
        decision_log = decision_log_id(command.principal_id, command.draft_id)
        while True:
            decisions = read_all(self._store, decision_log)
            if not decisions:
                if command.kind != "draft.beginEditing":
                    return DraftResult(
                        "invalid", None, message="Draft is not initialized"
                    )
                initialized = self.initialize(
                    command.principal_id,
                    command.draft_id,
                    command.expected_head,
                )
                if initialized.kind not in ("accepted", "current"):
                    return DraftResult(
                        "invalid",
                        None,
                        message=initialized.message or "Draft could not be initialized",
                    )
                continue
            ref = project_draft(decisions)
            replayed = replayed_command(decisions, command.command_id)
            if replayed is not None:
                prior_request, prior_decision, prior_ref, prior_kind = replayed
                if prior_request != command.request:
                    return DraftResult(
                        "invalid",
                        ref,
                        message="commandId was reused by another request",
                    )
                return DraftResult(prior_kind, prior_ref, prior_decision)
            outcome = self._decide(command, ref)
            if isinstance(outcome, DraftResult):
                return outcome
            result = self._commit(decisions, command, ref, outcome)
            if result is not None:
                return result

    def _decide(
        self,
        command: _Command,
        ref: DraftRef,
    ) -> _Decision | DraftResult:
        expected_epoch = command.expected_epoch
        if type(expected_epoch) is not int or expected_epoch < 0:
            return _Decision(
                "invalid",
                message="command.expectedEpoch must be non-negative",
            )
        if command.kind == "draft.beginEditing":
            return self._begin_editing(command, ref, expected_epoch)
        assert command.target_head is not None
        target = find_record(self._store, command.target_head)
        if target is None:
            return DraftResult(
                "missingDependency",
                ref,
                message="referenced Revision is not available",
            )
        try:
            revision = decode_revision(target)
        except (TypeError, ValueError) as error:
            return DraftResult("invalid", ref, message=str(error))
        if (
            revision["principalId"] != command.principal_id
            or revision["draftId"] != command.draft_id
        ):
            return DraftResult(
                "invalid",
                ref,
                message="Revision belongs to another Principal or Draft",
            )
        if command.kind == "draft.takeover":
            return self._takeover(command, ref, expected_epoch, revision)
        return self._advance(command, ref, expected_epoch, revision)

    def _begin_editing(
        self,
        command: _Command,
        ref: DraftRef,
        expected_epoch: int,
    ) -> _Decision:
        if ref.writer is not None and ref.writer != command.replica_id:
            return _Decision("activeElsewhere")
        if command.expected_head != ref.head or expected_epoch != ref.epoch:
            return _Decision("changed")
        if ref.writer == command.replica_id:
            return _Decision("current")
        return _Decision("accepted", {
            "type": "draft.editingStarted",
            "draftId": command.draft_id,
            "commandId": command.command_id,
            "request": command.request.to_value(),
            "writer": command.replica_id,
            "epoch": ref.epoch + 1,
        })

    def _takeover(
        self,
        command: _Command,
        ref: DraftRef,
        expected_epoch: int,
        revision: Mapping[str, object],
    ) -> _Decision:
        if command.expected_head != ref.head or expected_epoch != ref.epoch:
            return _Decision("changed")
        if revision["replicaId"] != command.replica_id:
            return _Decision(
                "invalid",
                message="takeover Revision belongs to another Replica",
            )
        assert command.target_head is not None
        return _Decision("accepted", {
            "type": "draft.takenOver",
            "draftId": command.draft_id,
            "commandId": command.command_id,
            "request": command.request.to_value(),
            "head": command.target_head.to_value(),
            "writer": command.replica_id,
            "epoch": ref.epoch + 1,
        })

    def _advance(
        self,
        command: _Command,
        ref: DraftRef,
        expected_epoch: int,
        revision: Mapping[str, object],
    ) -> _Decision:
        if command.expected_head != ref.head or expected_epoch != ref.epoch:
            return _Decision("changed")
        if ref.writer != command.replica_id or revision["replicaId"] != command.replica_id:
            return _Decision("changed")
        if revision["base"] != command.expected_head:
            return _Decision(
                "invalid",
                message="Revision was not edited from expectedHead",
            )
        assert command.target_head is not None
        return _Decision("accepted", {
            "type": "draft.advanced",
            "draftId": command.draft_id,
            "commandId": command.command_id,
            "request": command.request.to_value(),
            "head": command.target_head.to_value(),
            "writer": command.replica_id,
            "epoch": expected_epoch,
        })

    def _commit(
        self,
        decisions: tuple[ReplicatedRecord, ...],
        command: _Command,
        ref: DraftRef,
        outcome: _Decision,
    ) -> DraftResult | None:
        if outcome.kind == "current":
            return DraftResult("current", ref)
        if outcome.data is None:
            rejected = self._reject(
                decisions,
                command.request,
                command.command_id,
                outcome.kind,
                ref,
            )
            if rejected is None:
                return None
            return DraftResult(
                outcome.kind,
                ref,
                rejected,
                outcome.message,
            )
        record = _record(
            decisions[-1].log_id,
            decisions[-1].position,
            (),
            outcome.data,
        )
        appended = self._store.append(
            record.log_id,
            decisions[-1].position,
            (record,),
        )
        if appended.kind != "accepted":
            return None
        return DraftResult(
            "accepted",
            project_draft(read_all(self._store, record.log_id)),
            record.position,
        )

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
