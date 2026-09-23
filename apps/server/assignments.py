"""Assignment：与 Playground 同一套发布机制，另一张表、另一套 ref。

内容（题目）住在 Git 里，这里只管"当前是哪个 Version、标题是什么、还上不上线"。
本阶段一个 Version 里只有一份 `assignment.md`；标题就是它的第一行。
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import database
import repository as _repository


class InvalidAssignmentError(ValueError):
    """上传的 Markdown 不符合本阶段的形状。"""


class MissingAssignmentError(Exception):
    """还没有这个 Assignment。"""


class AssignmentBaseChangedError(Exception):
    """上传时基于的 Version 已经不是当前版本。"""


#: 一份题目的上限。够写一页要求，也不至于让人塞一本书进来。
MAX_MARKDOWN_BYTES = 64 * 1024
#: 标题长度，与课程标题同一上限。
MAX_TITLE_LENGTH = 200

#: 第一行的形状：`#` + 空白 + 标题。
_TITLE_LINE = re.compile(r"\A#[ \t]+(.+?)[ \t]*\Z")
_VERSION_ID_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z|\A[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class Assignment:
    id: str
    created_at: str
    state: str = "active"
    current_version_id: str = ""
    title: str = ""


def decode_markdown(payload):
    """Validate one uploaded Markdown and return its title.

    The whole requirement of this stage lives here: UTF-8, bounded, and the
    first line is a `# 标题` whose text is 1..200 characters. Everything else in
    the file is kept as it was and not read.
    """

    if type(payload) is not bytes or len(payload) == 0:
        raise InvalidAssignmentError("assignment must be a non-empty body")
    if len(payload) > MAX_MARKDOWN_BYTES:
        raise InvalidAssignmentError("assignment is too large")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidAssignmentError("assignment is not UTF-8")

    first = text.split("\n", 1)[0].rstrip("\r")
    match = _TITLE_LINE.match(first)
    if match is None:
        raise InvalidAssignmentError("the first line is not a title")
    title = match.group(1).strip()
    if not title or len(title) > MAX_TITLE_LENGTH:
        raise InvalidAssignmentError("title must be 1..200 characters")
    return title


def _require_id(value):
    if not _repository.valid_playground_id(value):
        raise InvalidAssignmentError("assignment_id is not valid")
    return value


def _assignment_row(connection, assignment_id):
    return connection.execute(
        "SELECT id, created_at, state, current_version_id, title FROM assignments"
        " WHERE id = ?",
        (assignment_id,),
    ).fetchone()


def get_assignment(connection, assignment_id):
    """The stored Assignment, or ``None``."""

    if not _repository.valid_playground_id(assignment_id):
        return None
    row = _assignment_row(connection, assignment_id)
    if row is None:
        return None
    return Assignment(
        id=row["id"],
        created_at=row["created_at"],
        state=row["state"],
        current_version_id=row["current_version_id"],
        title=row["title"],
    )


def require_publishable(connection, assignment_id, base_version_id):
    """Return the row a publish has to build on, refusing a stale base.

    There is no "stopped" refusal here on purpose: publishing an Assignment that
    was unpublished is how it comes back, so a stopped row is a normal base.
    """

    assignment_id = _require_id(assignment_id)
    row = _assignment_row(connection, assignment_id)
    current = row["current_version_id"] if row is not None else ""

    if base_version_id and not _VERSION_ID_PATTERN.match(base_version_id):
        raise InvalidAssignmentError("base_version_id is not a version id")
    if not current:
        if base_version_id:
            raise AssignmentBaseChangedError("assignment has no version to base on")
        return row
    if not base_version_id:
        raise InvalidAssignmentError("base_version_id is required")
    if base_version_id != current:
        raise AssignmentBaseChangedError("base_version_id is not current")
    return row


def observed_version(row):
    """The Version a publish has to build on, or ``None`` for the first one."""

    if row is None:
        return None
    return row["current_version_id"] or None


def commit_prepared_assignment(connection, assignment_id, prepared, base_version_id, title):
    """Accept a prepared Version: CAS the current Version, set the title, go active.

    The whole database half of a publish, in the caller's transaction. A stopped
    Assignment comes back with this same write.
    """

    row = _assignment_row(connection, assignment_id)
    if row is None:
        if base_version_id:
            raise AssignmentBaseChangedError("assignment has no history to base on")
        connection.execute(
            "INSERT INTO assignments (id, created_at, state, current_version_id, title)"
            " VALUES (?, ?, 'active', ?, ?)",
            (assignment_id, _now(), prepared.commit_id, title),
        )
        return prepared.commit_id

    if row["current_version_id"] != (base_version_id or ""):
        raise AssignmentBaseChangedError("base_version_id is not current")
    connection.execute(
        "UPDATE assignments SET current_version_id = ?, title = ?, state = 'active'"
        " WHERE id = ?",
        (prepared.commit_id, title, assignment_id),
    )
    return prepared.commit_id


def unpublish(connection, assignment_id):
    """Take an Assignment off the students' list.

    Returns whether it changed anything: ``False`` for one that is already
    stopped. Versions are untouched — this is "stop handing it out", not
    "delete the question".
    """

    assignment_id = _require_id(assignment_id)

    with database.immediate_transaction(connection):
        row = _assignment_row(connection, assignment_id)
        if row is None:
            raise MissingAssignmentError("assignment does not exist")
        if row["state"] == "stopped":
            return False
        connection.execute(
            "UPDATE assignments SET state = 'stopped' WHERE id = ?",
            (assignment_id,),
        )
        return True


def active_assignments(connection):
    """Every active Assignment, in the order they were first published."""

    rows = connection.execute(
        "SELECT id, current_version_id, title FROM assignments"
        " WHERE state = 'active' ORDER BY created_at, id"
    ).fetchall()
    return [
        {
            "assignment_id": row["id"],
            "current_version_id": row["current_version_id"],
            "title": row["title"],
        }
        for row in rows
        if row["current_version_id"]
    ]


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
