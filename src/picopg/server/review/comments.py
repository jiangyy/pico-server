"""Server-owned review comments for a Draft.

The comment Log carries facts the Server produces for a user to read; Clients never
append to it. One Log per Draft, so read grants stay per Draft like the Decision Log.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Mapping, Protocol, Union

from picopg.server.common.ids import encode_component
from picopg.server.common.reads import PaginationStalled, read_all
from picopg.server.common.validation import optional_time, require_non_blank_position, require_non_blank_text, utc_now
from picopg.replica import Position, ReplicaLogStore, ReplicatedRecord, record_id_of


COMMENT_KIND = "comment.added"
#: note 旁注、strike 划掉、underline 波浪线、suggest 建议改写、praise 表扬；和客户端的 ReviewCommentKind 一致。
REVIEW_KINDS = frozenset(("note", "strike", "underline", "suggest", "praise"))
MAX_QUOTE = 4_000
MAX_REPLACEMENT = 4_000
_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
JsonValue = Union[None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]]


class CommentError(ValueError):
    """Raised when a comment cannot be accepted as a fact."""


def comment_log_id(principal_id: str, draft_id: str) -> str:
    return (
        f"principal/{encode_component(principal_id, 'principalId')}"
        f"/draft/{encode_component(draft_id, 'draftId')}/comment"
    )


@dataclass(frozen=True, slots=True)
class Comment:
    position: Position
    draft_id: str
    author: str
    text: str
    submission_id: str | None = None
    submission_request: Position | None = None
    revision: Position | None = None
    # 评语写下的服务端时间；这个字段加入之前的旧评语没有。
    created_at: str | None = None
    # 批在代码上的意见：类型、批在哪段代码上、建议的写法；总评三者都没有。
    kind: str | None = None
    anchor: Mapping[str, object] | None = None
    replacement: str | None = None

    def review_value(self) -> dict[str, object]:
        """客户端读的那几项：作者、正文、时间，以及批在代码上的类型、锚点和建议写法。"""
        value: dict[str, object] = {"author": self.author, "text": self.text}
        if self.created_at is not None:
            value["createdAt"] = self.created_at
        if self.kind is not None:
            value["kind"] = self.kind
        if self.anchor is not None:
            value["anchor"] = _plain_anchor(self.anchor)
        if self.replacement is not None:
            value["replacement"] = self.replacement
        return value


class CommentServer(Protocol):
    @property
    def store(self) -> ReplicaLogStore: ...

    def append_and_broadcast(
        self, log_id: str, expected_head: Position | None, records: tuple[ReplicatedRecord, ...]
    ) -> object: ...


def _draft_id(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise CommentError("draftId is not a valid identifier")
    return value


class CommentPublisher:
    """Appends comments to the Draft's comment Log, which Clients only read."""

    def __init__(self, server: CommentServer, clock: Callable[[], str] = utc_now) -> None:
        self._server = server
        self._store = server.store
        self._lock = threading.Lock()
        self._clock = clock

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
        review: Mapping[str, object] | None = None,
    ) -> Position:
        """review 是批在代码上的那几项（kind、anchor、replacement），先校验再写进记录。"""
        return self._add(
            principal_id,
            draft_id,
            author,
            text,
            submission_id=require_non_blank_text(
                submission_id,
                "submissionId",
                error_type=CommentError,
            ),
            submission_request=submission_request,
            revision=revision,
            review=review_fields(review or {}),
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
        review: Mapping[str, object] | None = None,
    ) -> Position:
        principal_id = require_non_blank_text(
            principal_id,
            "principalId",
            error_type=CommentError,
        )
        draft_id = _draft_id(draft_id)
        author = require_non_blank_text(author, "author", error_type=CommentError)
        text = require_non_blank_text(text, "text", error_type=CommentError)
        log_id = comment_log_id(principal_id, draft_id)
        data: dict[str, JsonValue] = {
            "type": COMMENT_KIND,
            "draftId": draft_id,
            "author": author,
            "text": text,
            # 写入时定下、之后随记录不可变；画像的时间窗口与“最近的评语”都以它为准。
            "createdAt": self._clock(),
            **(review or {}),
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
        principal_id = require_non_blank_text(
            principal_id,
            "principalId",
            error_type=CommentError,
        )
        draft_id = _draft_id(draft_id)
        log_id = comment_log_id(principal_id, draft_id)
        comments: list[Comment] = []
        try:
            records = read_all(self._store, log_id)
        except PaginationStalled as error:
            raise RuntimeError(
                "comment Log returned an empty continuation page"
            ) from error
        for record in records:
            comments.append(_decode(record, draft_id))
        return tuple(comments)


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
        submission_id = require_non_blank_text(
            data.get("submissionId"),
            "submissionId",
            error_type=CommentError,
        )
        submission_request = require_non_blank_position(
            data.get("submissionRequest"),
            "submissionRequest",
            error_type=CommentError,
        )
        revision = require_non_blank_position(
            data.get("revision"),
            "revision",
            error_type=CommentError,
        )
    return Comment(
        record.position,
        draft_id,
        require_non_blank_text(
            data.get("author"), "author", error_type=CommentError
        ),
        require_non_blank_text(
            data.get("text"), "text", error_type=CommentError
        ),
        submission_id,
        submission_request,
        revision,
        optional_time(data.get("createdAt"), "createdAt", error_type=CommentError),
        **_review_of(review_fields({key: data[key] for key in ("kind", "anchor", "replacement") if key in data})),
    )


def review_fields(value: Mapping[str, object]) -> dict[str, object]:
    """校验批在代码上的那几项，返回可以写进记录的普通 JSON；总评返回空。"""
    unknown = set(value) - {"kind", "anchor", "replacement"}
    if unknown:
        raise CommentError(f"unknown comment fields: {', '.join(sorted(unknown))}")
    fields: dict[str, object] = {}
    kind = value.get("kind")
    if kind is not None:
        if kind not in REVIEW_KINDS:
            raise CommentError("comment kind is invalid")
        fields["kind"] = kind
    anchor = value.get("anchor")
    if anchor is not None:
        if not isinstance(anchor, Mapping) or set(anchor) != {"from", "to", "quote"}:
            raise CommentError("comment anchor must contain from, to and quote")
        start, end = _point(anchor["from"]), _point(anchor["to"])
        if (end["line"], end["column"]) < (start["line"], start["column"]):
            raise CommentError("comment anchor ends before it starts")
        quote = anchor["quote"]
        if not isinstance(quote, str) or not quote or len(quote) > MAX_QUOTE:
            raise CommentError("comment anchor quote is invalid")
        fields["anchor"] = {"from": start, "to": end, "quote": quote}
    replacement = value.get("replacement")
    if replacement is not None:
        if kind != "suggest":
            raise CommentError("only a suggest comment carries a replacement")
        if not isinstance(replacement, str) or len(replacement) > MAX_REPLACEMENT:
            raise CommentError("comment replacement is invalid")
        fields["replacement"] = replacement
    if kind is not None and anchor is None:
        raise CommentError("a typed comment must be anchored to code")
    return fields


def _point(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != {"line", "column"}:
        raise CommentError("comment anchor point must contain line and column")
    line, column = value["line"], value["column"]
    if type(line) is not int or type(column) is not int or line < 1 or column < 1:
        raise CommentError("comment anchor point is invalid")
    return {"line": line, "column": column}


def _review_of(fields: Mapping[str, object]) -> dict[str, object]:
    return {
        "kind": fields.get("kind"),
        "anchor": fields.get("anchor"),
        "replacement": fields.get("replacement"),
    }


def _plain_anchor(anchor: Mapping[str, object]) -> dict[str, object]:
    start, end = anchor["from"], anchor["to"]
    assert isinstance(start, Mapping) and isinstance(end, Mapping)
    return {"from": dict(start), "to": dict(end), "quote": anchor["quote"]}
