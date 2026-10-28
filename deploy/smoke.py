"""Exercise the deployed HTTPS, Session, SSE, Draft, Submission and review flow."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urljoin, urlsplit
from uuid import uuid4


# 直接以脚本运行（部署机上没有安装 picopg）：从仓库的 src/ 导入。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from picopg.server.drafts.protocol import (  # noqa: E402
    decision_log_id,
    project_draft,
    request_log_id,
    revision_log_id,
)
from picopg.server.common.ids import encode_component  # noqa: E402
from picopg.server.review.comments import comment_log_id  # noqa: E402
from picopg.server.submissions.logs import result_log  # noqa: E402
from picopg.replica import Position, ReplicatedRecord, record_id_of  # noqa: E402


@dataclass(frozen=True, slots=True)
class SmokeState:
    principal_id: str
    replica_id: str
    session_cookie: str
    device_cookie: str
    playground_id: str
    source: str
    revision_record_id: str
    command_id: str
    submission_id: str
    request_record_id: str
    tag: str
    comment_text: str

    @classmethod
    def from_value(cls, value: object) -> "SmokeState":
        fields = tuple(cls.__dataclass_fields__)
        if not isinstance(value, dict) or set(value) != set(fields):
            raise ValueError("smoke state has an invalid shape")
        if any(not isinstance(value[field], str) or not value[field] for field in fields):
            raise ValueError("smoke state fields must be non-empty strings")
        return cls(**{field: value[field] for field in fields})


@dataclass(frozen=True, slots=True)
class SmokeResult:
    principal_id: str
    replica_id: str
    submissions: int
    tag: str


def _cookie(name: str, value: str, base_url: str) -> http.cookiejar.Cookie:
    parsed = urlsplit(base_url)
    assert parsed.hostname is not None
    return http.cookiejar.Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=parsed.hostname,
        domain_specified=False,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=parsed.scheme == "https",
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None, "SameSite": "Strict"},
        rfc2109=False,
    )


def _opener(
    base_url: str,
    cookies: dict[str, str] | None = None,
) -> tuple[urllib.request.OpenerDirector, http.cookiejar.CookieJar]:
    jar = http.cookiejar.CookieJar()
    for name, value in (cookies or {}).items():
        jar.set_cookie(_cookie(name, value, base_url))
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def _json(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: object | None = None,
) -> tuple[object, urllib.response.addinfourl]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {} if payload is None else {"Content-Type": "application/json"}
    request = urllib.request.Request(
        urljoin(base_url, path), data=payload, headers=headers, method=method
    )
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} returned HTTP {error.code}: {detail}") from error
    with response:
        return json.loads(response.read() or b"null"), response


def _records(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    log_id: str,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    after: str | None = None
    while True:
        query = "?limit=128" + ("" if after is None else f"&after={quote(after, safe='')}")
        page, _ = _json(
            opener,
            base_url,
            f"/api/v1/replica/logs/{quote(log_id, safe='')}/records{query}",
        )
        if not isinstance(page, dict) or not isinstance(page.get("records"), list):
            raise RuntimeError(f"{log_id} returned an invalid Log page")
        batch = page["records"]
        if any(not isinstance(record, dict) for record in batch):
            raise RuntimeError(f"{log_id} returned an invalid Record")
        records.extend(batch)
        if page.get("hasMore") is not True:
            return records
        if not batch or not isinstance(batch[-1].get("id"), str):
            raise RuntimeError(f"{log_id} returned an empty continuation page")
        after = batch[-1]["id"]


def _head(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    log_id: str,
) -> Position | None:
    value, _ = _json(
        opener,
        base_url,
        f"/api/v1/replica/logs/{quote(log_id, safe='')}/head",
    )
    if not isinstance(value, dict) or set(value) != {"head"}:
        raise RuntimeError(f"{log_id} returned an invalid head")
    return None if value["head"] is None else Position.from_value(value["head"], log_id=log_id)


def _record(
    log_id: str,
    parent: Position | None,
    requires: tuple[Position, ...],
    data: dict[str, object],
) -> ReplicatedRecord:
    return ReplicatedRecord(
        record_id_of(log_id, parent, requires, data),
        log_id,
        parent,
        requires,
        data,
    )


def _append(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    record: ReplicatedRecord,
) -> None:
    value, _ = _json(
        opener,
        base_url,
        f"/api/v1/replica/logs/{quote(record.log_id, safe='')}/records",
        method="POST",
        body={
            "expectedHead": None if record.parent is None else record.parent.to_value(),
            "records": [record.to_value()],
        },
    )
    if not isinstance(value, dict) or value.get("kind") != "accepted":
        raise RuntimeError(f"append to {record.log_id} was not accepted: {value!r}")


def _events(
    opener: urllib.request.OpenerDirector,
    base_url: str,
) -> urllib.response.addinfourl:
    response = opener.open(
        urllib.request.Request(urljoin(base_url, "/api/v1/replica/events")),
        timeout=10,
    )
    if response.headers.get_content_type() != "text/event-stream":
        response.close()
        raise RuntimeError("SSE response has the wrong content type")
    if response.readline() != b": connected\n" or response.readline() != b"\n":
        response.close()
        raise RuntimeError("SSE proxy did not flush the connection prelude")
    return response


def _expect_event(events: urllib.response.addinfourl, log_id: str) -> None:
    try:
        for _ in range(12):
            line = events.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                payload = json.loads(
                    line[6:].decode("utf-8", errors="strict").rstrip("\r\n")
                )
                if isinstance(payload, dict) and payload.get("logId") == log_id:
                    if events.readline() != b"\n":
                        raise RuntimeError("SSE event is missing its frame terminator")
                    return
    except (TimeoutError, socket.timeout) as error:
        raise RuntimeError(f"SSE did not notify {log_id}") from error
    raise RuntimeError(f"SSE did not notify {log_id}")


def _wait_record(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    log_id: str,
    predicate: Callable[[dict[str, object]], bool],
    message: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for record in _records(opener, base_url, log_id):
            if predicate(record):
                return record
        time.sleep(0.05)
    raise RuntimeError(message)


def _preflight(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    playground_id: str,
    *,
    check_static: bool,
) -> None:
    health, _ = _json(opener, base_url, "/healthz")
    if health != {"status": "ok"}:
        raise RuntimeError("health response is invalid")
    if check_static:
        for path in ("/client/", "/teacher/"):
            with opener.open(urljoin(base_url, path), timeout=10) as response:
                if response.status != 200 or b"<html" not in response.read(4096).lower():
                    raise RuntimeError(f"{path} is not a built HTML entry")
    catalog, _ = _json(opener, base_url, "/api/v1/catalog")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("playgrounds"), list):
        raise RuntimeError("catalog response is invalid")
    release = next((
        item for item in catalog["playgrounds"]
        if isinstance(item, dict) and item.get("playground_id") == playground_id
    ), None)
    if release is None or not isinstance(release.get("current_version_id"), str):
        raise RuntimeError(f"smoke Playground {playground_id!r} is not published")
    package_path = (
        f"/api/v1/playgrounds/{quote(playground_id, safe='')}/versions/"
        f"{quote(release['current_version_id'], safe='')}/package"
    )
    with opener.open(urljoin(base_url, package_path), timeout=10) as response:
        if response.status != 200 or response.headers.get_content_type() != "application/zip":
            raise RuntimeError("smoke Playground package response is invalid")
        if not response.read(1):
            raise RuntimeError("smoke Playground package is empty")


def _bootstrap(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    access_code: str,
    scheme: str,
) -> tuple[str, str]:
    session, response = _json(
        opener,
        base_url,
        "/api/v1/session",
        method="POST",
        body={"accessCode": access_code},
    )
    joined_cookies = "\n".join(response.headers.get_all("Set-Cookie", []))
    if "HttpOnly" not in joined_cookies or "SameSite=Strict" not in joined_cookies:
        raise RuntimeError("Session cookies are missing HttpOnly or SameSite=Strict")
    if scheme == "https" and "Secure" not in joined_cookies:
        raise RuntimeError("HTTPS Session cookies are missing Secure")
    if not isinstance(session, dict):
        raise RuntimeError("Session response is invalid")
    principal_id = session.get("principalId")
    replica_id = session.get("replicaId")
    if not isinstance(principal_id, str) or not isinstance(replica_id, str):
        raise RuntimeError("Session identity is invalid")
    current, _ = _json(opener, base_url, "/api/v1/session")
    if current != session:
        raise RuntimeError("Session did not survive its bootstrap request")
    return principal_id, replica_id


def _operator_login(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    operator_token: str,
) -> None:
    _json(
        opener,
        base_url,
        "/api/v1/operator/session",
        method="POST",
        body={"token": operator_token},
    )


def prepare(
    base_url: str,
    access_code: str,
    operator_token: str,
    playground_id: str,
    *,
    allow_http: bool = False,
    check_static: bool = True,
) -> tuple[SmokeState, SmokeResult]:
    base_url = base_url.rstrip("/") + "/"
    scheme = urlsplit(base_url).scheme
    if scheme != "https" and not (allow_http and scheme == "http"):
        raise ValueError("base URL must use HTTPS (or pass allow_http for a local test)")
    opener, cookies = _opener(base_url)
    principal_id, replica_id = _bootstrap(opener, base_url, access_code, scheme)
    _preflight(opener, base_url, playground_id, check_static=check_static)
    events = _events(opener, base_url)
    nonce = uuid4().hex
    source = f'# PicoPG deployment smoke {nonce}\nprint("picopg smoke {nonce}")\n'
    revision_log = revision_log_id(principal_id, playground_id, replica_id)
    request_log = request_log_id(principal_id, playground_id, replica_id)
    decisions_log = decision_log_id(principal_id, playground_id)
    submission_log = (
        f"principal/{encode_component(principal_id, 'principalId')}/submission/"
        f"{encode_component(replica_id, 'replicaId')}/requests"
    )
    results_log = result_log(submission_log)
    try:
        decision_values = _records(opener, base_url, decisions_log)
        decisions = tuple(ReplicatedRecord.from_value(value) for value in decision_values)
        current = None if not decisions else project_draft(decisions)
        revision = _record(
            revision_log,
            _head(opener, base_url, revision_log),
            (),
            {
                "type": "draft.revised",
                "draftId": playground_id,
                "replicaId": replica_id,
                "source": source,
                "base": None if current is None else current.head.to_value(),
            },
        )
        _append(opener, base_url, revision)
        _expect_event(events, revision_log)

        command_id = f"smoke-command-{nonce}"
        if current is None:
            command_type = "draft.beginEditing"
            requires = (revision.position,)
            command_data: dict[str, object] = {
                "type": command_type,
                "commandId": command_id,
                "draftId": playground_id,
                "replicaId": replica_id,
                "expectedHead": revision.position.to_value(),
                "expectedEpoch": 0,
            }
            expected_decision = "draft.editingStarted"
        else:
            command_type = "draft.advance" if current.writer == replica_id else "draft.takeover"
            requires = (current.head, revision.position)
            command_data = {
                "type": command_type,
                "commandId": command_id,
                "draftId": playground_id,
                "replicaId": replica_id,
                "expectedHead": current.head.to_value(),
                "expectedEpoch": current.epoch,
                "targetHead": revision.position.to_value(),
            }
            expected_decision = (
                "draft.advanced" if command_type == "draft.advance" else "draft.takenOver"
            )
        command = _record(
            request_log,
            _head(opener, base_url, request_log),
            requires,
            command_data,
        )
        _append(opener, base_url, command)
        _expect_event(events, decisions_log)
        decision = _wait_record(
            opener,
            base_url,
            decisions_log,
            lambda record: isinstance(record.get("data"), dict)
            and record["data"].get("commandId") == command_id,
            "Draft worker did not produce a decision",
        )
        if decision["data"].get("type") != expected_decision:  # type: ignore[union-attr]
            raise RuntimeError(f"Draft command was not accepted: {decision['data']!r}")

        submission_id = f"smoke-submission-{nonce}"
        submission = _record(
            submission_log,
            _head(opener, base_url, submission_log),
            (revision.position,),
            {
                "type": "submission.requested",
                "submissionId": submission_id,
                "playgroundId": playground_id,
                "revision": revision.position.to_value(),
            },
        )
        _append(opener, base_url, submission)
        _expect_event(events, results_log)
        result = _wait_record(
            opener,
            base_url,
            results_log,
            lambda record: isinstance(record.get("data"), dict)
            and record["data"].get("request") == submission.position.to_value(),
            "Submission worker did not produce a result",
        )
        result_data = result["data"]
        if not isinstance(result_data, dict) or result_data.get("type") != "submission.completed":
            raise RuntimeError(f"Submission did not complete: {result_data!r}")
        tag = result_data.get("tag")
        if not isinstance(tag, str) or not tag:
            raise RuntimeError("Submission result is missing its Git tag")

        _operator_login(opener, base_url, operator_token)
        listing, _ = _json(opener, base_url, "/api/v1/operator/submissions")
        if not isinstance(listing, dict) or not isinstance(listing.get("submissions"), list):
            raise RuntimeError("Submission list response is invalid")
        summary = next((
            item for item in listing["submissions"]
            if isinstance(item, dict) and item.get("requestRecordId") == submission.id
        ), None)
        if summary is None or summary.get("tag") != tag:
            raise RuntimeError("operator Submission list is missing the smoke Submission")
        detail_path = f"/api/v1/operator/submissions/{quote(submission.id, safe='')}"
        detail, _ = _json(opener, base_url, detail_path)
        if not isinstance(detail, dict) or detail.get("source") != source:
            raise RuntimeError("operator Submission detail did not preserve exact source")
        comment_text = f"deployment smoke comment {nonce}"
        comment, _ = _json(
            opener,
            base_url,
            f"{detail_path}/comments",
            method="POST",
            body={"author": "deployment-smoke", "text": comment_text},
        )
        if not isinstance(comment, dict) or not isinstance(comment.get("position"), dict):
            raise RuntimeError("operator comment response is invalid")
        _expect_event(events, comment_log_id(principal_id, playground_id))
        history, _ = _json(opener, base_url, "/api/v1/submissions")
        if not isinstance(history, dict) or not isinstance(history.get("submissions"), list):
            raise RuntimeError("student Submission history is invalid")
        student_item = next((
            item for item in history["submissions"]
            if isinstance(item, dict) and item.get("id") == submission.id
        ), None)
        expected_comment = {"author": "deployment-smoke", "text": comment_text}
        if student_item is None or expected_comment not in student_item.get("comments", []):
            raise RuntimeError("teacher comment did not reach student Submission history")
        _json(opener, base_url, "/api/v1/operator/session", method="DELETE")
    finally:
        events.close()

    cookie_values = {cookie.name: cookie.value for cookie in cookies}
    session_cookie = cookie_values.get("picopg_session")
    device_cookie = cookie_values.get("picopg_device")
    if not session_cookie or not device_cookie:
        raise RuntimeError("Session cookies were not retained")
    state = SmokeState(
        principal_id,
        replica_id,
        session_cookie,
        device_cookie,
        playground_id,
        source,
        revision.id,
        command_id,
        submission_id,
        submission.id,
        tag,
        comment_text,
    )
    return state, SmokeResult(principal_id, replica_id, len(listing["submissions"]), tag)


def verify(
    base_url: str,
    operator_token: str,
    state: SmokeState,
    *,
    allow_http: bool = False,
    check_static: bool = True,
) -> SmokeResult:
    base_url = base_url.rstrip("/") + "/"
    scheme = urlsplit(base_url).scheme
    if scheme != "https" and not (allow_http and scheme == "http"):
        raise ValueError("base URL must use HTTPS (or pass allow_http for a local test)")
    opener, _ = _opener(base_url, {
        "picopg_session": state.session_cookie,
        "picopg_device": state.device_cookie,
    })
    current, _ = _json(opener, base_url, "/api/v1/session")
    expected_identity = {
        "principalId": state.principal_id,
        "replicaId": state.replica_id,
    }
    if current != expected_identity:
        raise RuntimeError("Session identity did not survive restart")
    _preflight(opener, base_url, state.playground_id, check_static=check_static)
    events = _events(opener, base_url)
    events.close()

    decisions_log = decision_log_id(state.principal_id, state.playground_id)
    if not any(
        isinstance(record.get("data"), dict)
        and record["data"].get("commandId") == state.command_id
        and record["data"].get("type")
        in ("draft.editingStarted", "draft.takenOver", "draft.advanced")
        for record in _records(opener, base_url, decisions_log)
    ):
        raise RuntimeError("Draft decision did not survive restart")

    history, _ = _json(opener, base_url, "/api/v1/submissions")
    if not isinstance(history, dict) or not isinstance(history.get("submissions"), list):
        raise RuntimeError("student Submission history is invalid after restart")
    student_item = next((
        item for item in history["submissions"]
        if isinstance(item, dict) and item.get("id") == state.request_record_id
    ), None)
    expected_comment = {"author": "deployment-smoke", "text": state.comment_text}
    if (
        student_item is None
        or student_item.get("status") != "completed"
        or student_item.get("tag") != state.tag
        or expected_comment not in student_item.get("comments", [])
    ):
        raise RuntimeError("Submission or teacher comment did not survive restart")

    _operator_login(opener, base_url, operator_token)
    listing, _ = _json(opener, base_url, "/api/v1/operator/submissions")
    if not isinstance(listing, dict) or not isinstance(listing.get("submissions"), list):
        raise RuntimeError("Submission list response is invalid after restart")
    detail, _ = _json(
        opener,
        base_url,
        f"/api/v1/operator/submissions/{quote(state.request_record_id, safe='')}",
    )
    expected_revision = {
        "logId": revision_log_id(
            state.principal_id, state.playground_id, state.replica_id
        ),
        "recordId": state.revision_record_id,
    }
    if (
        not isinstance(detail, dict)
        or detail.get("source") != state.source
        or detail.get("revision") != expected_revision
        or detail.get("tag") != state.tag
        or not any(
            isinstance(comment, dict) and comment.get("text") == state.comment_text
            for comment in detail.get("comments", [])
        )
    ):
        raise RuntimeError("operator Submission detail did not survive restart")
    _json(opener, base_url, "/api/v1/operator/session", method="DELETE")
    _json(opener, base_url, "/api/v1/session", method="DELETE")
    try:
        _json(opener, base_url, "/api/v1/session")
    except RuntimeError as error:
        if "HTTP 401" not in str(error):
            raise
    else:
        raise RuntimeError("Session remained authorized after logout")
    return SmokeResult(
        state.principal_id,
        state.replica_id,
        len(listing["submissions"]),
        state.tag,
    )


def run(
    base_url: str,
    access_code: str,
    operator_token: str,
    playground_id: str,
    *,
    allow_http: bool = False,
    check_static: bool = True,
) -> SmokeResult:
    state, _ = prepare(
        base_url,
        access_code,
        operator_token,
        playground_id,
        allow_http=allow_http,
        check_static=check_static,
    )
    return verify(
        base_url,
        operator_token,
        state,
        allow_http=allow_http,
        check_static=check_static,
    )


def _write_state(path: Path, state: SmokeState) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(asdict(state), output, separators=(",", ":"))
        output.write("\n")


def _read_state(path: Path) -> SmokeState:
    return SmokeState.from_value(json.loads(path.read_text(encoding="utf-8")))


def _print_result(result: SmokeResult, phase: str) -> None:
    print(
        f"smoke {phase}: principal={result.principal_id} "
        f"replica={result.replica_id} submissions={result.submissions} tag={result.tag}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--access-code", default=os.environ.get("PICOPG_SMOKE_ACCESS_CODE")
    )
    parser.add_argument(
        "--operator-token", default=os.environ.get("PICOPG_REPLICA_TOKEN")
    )
    parser.add_argument(
        "--playground-id", default=os.environ.get("PICOPG_SMOKE_PLAYGROUND_ID")
    )
    phases = parser.add_mutually_exclusive_group()
    phases.add_argument("--prepare-state", type=Path)
    phases.add_argument("--verify-state", type=Path)
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--skip-static", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.operator_token:
        parser.error("set PICOPG_REPLICA_TOKEN or pass --operator-token")
    if arguments.verify_state is not None:
        state = _read_state(arguments.verify_state)
        result = verify(
            arguments.base_url,
            arguments.operator_token,
            state,
            allow_http=arguments.allow_http,
            check_static=not arguments.skip_static,
        )
        _print_result(result, "verified after restart")
        return 0
    if not arguments.access_code:
        parser.error("set PICOPG_SMOKE_ACCESS_CODE or pass --access-code")
    if not arguments.playground_id:
        parser.error("set PICOPG_SMOKE_PLAYGROUND_ID or pass --playground-id")
    if arguments.prepare_state is not None:
        state, result = prepare(
            arguments.base_url,
            arguments.access_code,
            arguments.operator_token,
            arguments.playground_id,
            allow_http=arguments.allow_http,
            check_static=not arguments.skip_static,
        )
        _write_state(arguments.prepare_state, state)
        _print_result(result, "prepared")
        return 0
    result = run(
        arguments.base_url,
        arguments.access_code,
        arguments.operator_token,
        arguments.playground_id,
        allow_http=arguments.allow_http,
        check_static=not arguments.skip_static,
    )
    _print_result(result, "passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
