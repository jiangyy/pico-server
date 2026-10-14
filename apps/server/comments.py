"""Server-owned review comments for a Draft.

The comment Log carries facts the Server produces for a user to read; Clients never
append to it. One Log per Draft, so read grants stay per Draft like the Decision Log.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Mapping, Protocol, Union
from urllib.parse import quote

from picopg.api import Position, ReplicaLogStore, ReplicatedRecord, record_id_of


COMMENT_KIND = "comment.added"
_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
JsonValue = Union[None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]]


class CommentError(ValueError):
    """Raised when a comment cannot be accepted as a fact."""


def comment_log_id(principal_id: str, draft_id: str) -> str:
    safe = "-_.!~*()"
    return f"principal/{quote(principal_id, safe=safe)}/draft/{quote(draft_id, safe=safe)}/comment"


@dataclass(frozen=True, slots=True)
class Comment:
    position: Position
    draft_id: str
    author: str
    text: str
    submission_id: str | None = None
    submission_request: Position | None = None
    revision: Position | None = None


class CommentServer(Protocol):
    @property
    def store(self) -> ReplicaLogStore: ...

    def append_and_broadcast(
        self, log_id: str, expected_head: Position | None, records: tuple[ReplicatedRecord, ...]
    ) -> object: ...


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommentError(f"{label} must be non-empty")
    return value


def _draft_id(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise CommentError("draftId is not a valid identifier")
    return value


class CommentPublisher:
    """Appends comments to the Draft's comment Log, which Clients only read."""

    def __init__(self, server: CommentServer) -> None:
        self._server = server
        self._store = server.store
        self._lock = threading.Lock()

    def add(self, principal_id: str, draft_id: str, author: str, text: str) -> Position:
        return self._add(principal_id, draft_id, author, text)

    def add_submission(
        self,
        principal_id: str,
        draft_id: str,
        author: str,
        text: str,
        submission_id: str,
        submission_request: Position,
        revision: Position,
    ) -> Position:
        return self._add(
            principal_id,
            draft_id,
            author,
            text,
            submission_id=_text(submission_id, "submissionId"),
            submission_request=submission_request,
            revision=revision,
        )

    def _add(
        self,
        principal_id: str,
        draft_id: str,
        author: str,
        text: str,
        *,
        submission_id: str | None = None,
        submission_request: Position | None = None,
        revision: Position | None = None,
    ) -> Position:
        principal_id = _text(principal_id, "principalId")
        draft_id = _draft_id(draft_id)
        author = _text(author, "author")
        text = _text(text, "text")
        log_id = comment_log_id(principal_id, draft_id)
        data: dict[str, JsonValue] = {
            "type": COMMENT_KIND,
            "draftId": draft_id,
            "author": author,
            "text": text,
        }
        targets = (submission_id, submission_request, revision)
        if any(target is not None for target in targets):
            if any(target is None for target in targets):
                raise CommentError("submission target must be complete")
            assert submission_request is not None and revision is not None
            data.update({
                "submissionId": submission_id,
                "submissionRequest": submission_request.to_value(),
                "revision": revision.to_value(),
            })
        with self._lock:
            parent = self._store.head(log_id)
            record = ReplicatedRecord(
                id=record_id_of(log_id, parent, (), data),
                log_id=log_id,
                parent=parent,
                requires=(),
                data=data,
            )
            result = self._server.append_and_broadcast(log_id, parent, (record,))
            if result.kind != "accepted":
                raise RuntimeError("comment Log changed during a locked append")
            return record.position

    def comments(self, principal_id: str, draft_id: str) -> tuple[Comment, ...]:
        """Reads the Draft's comments in Log order."""
        principal_id = _text(principal_id, "principalId")
        draft_id = _draft_id(draft_id)
        log_id = comment_log_id(principal_id, draft_id)
        comments: list[Comment] = []
        after: Position | None = None
        while True:
            page = self._store.read(log_id, after, 128)
            for record in page.records:
                comments.append(_decode(record, draft_id))
            if not page.has_more:
                return tuple(comments)
            if not page.records:
                raise RuntimeError("comment Log returned an empty continuation page")
            after = page.records[-1].position


def _decode(record: ReplicatedRecord, draft_id: str) -> Comment:
    data = record.data
    if not isinstance(data, Mapping) or data.get("type") != COMMENT_KIND:
        raise CommentError("record is not a comment")
    if data.get("draftId") != draft_id:
        raise CommentError("comment belongs to another Draft")
    target_keys = {"submissionId", "submissionRequest", "revision"}
    present = target_keys.intersection(data)
    if present and present != target_keys:
        raise CommentError("submission target is incomplete")
    submission_id = None
    submission_request = None
    revision = None
    if present:
        submission_id = _text(data.get("submissionId"), "submissionId")
        submission_request = _position(data.get("submissionRequest"), "submissionRequest")
        revision = _position(data.get("revision"), "revision")
    return Comment(
        record.position,
        draft_id,
        _text(data.get("author"), "author"),
        _text(data.get("text"), "text"),
        submission_id,
        submission_request,
        revision,
    )


def _position(value: object, label: str) -> Position:
    if not isinstance(value, Mapping) or set(value) != {"logId", "recordId"}:
        raise CommentError(f"{label} must contain logId and recordId")
    return Position(
        _text(value["logId"], f"{label}.logId"),
        _text(value["recordId"], f"{label}.recordId"),
    )
