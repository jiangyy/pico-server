"""Exercise the deployed HTTPS, Session, SSE and teacher API surface."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit


@dataclass(frozen=True, slots=True)
class SmokeResult:
    principal_id: str
    replica_id: str
    submissions: int


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


def run(
    base_url: str,
    access_code: str,
    operator_token: str,
    *,
    allow_http: bool = False,
    check_static: bool = True,
) -> SmokeResult:
    base_url = base_url.rstrip("/") + "/"
    scheme = urlsplit(base_url).scheme
    if scheme != "https" and not (allow_http and scheme == "http"):
        raise ValueError("base URL must use HTTPS (or pass allow_http for a local test)")
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))

    health, _ = _json(opener, base_url, "/healthz")
    if health != {"status": "ok"}:
        raise RuntimeError("health response is invalid")
    if check_static:
        for path in ("/client/", "/teacher/"):
            with opener.open(urljoin(base_url, path), timeout=10) as response:
                if response.status != 200 or b"<html" not in response.read(4096).lower():
                    raise RuntimeError(f"{path} is not a built HTML entry")

    session, response = _json(
        opener, base_url, "/api/v1/session",
        method="POST", body={"accessCode": access_code},
    )
    cookie_headers = response.headers.get_all("Set-Cookie", [])
    joined_cookies = "\n".join(cookie_headers)
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
    catalog, _ = _json(opener, base_url, "/api/v1/catalog")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("playgrounds"), list):
        raise RuntimeError("catalog response is invalid")

    events = opener.open(
        urllib.request.Request(urljoin(base_url, "/api/v1/replica/events")),
        timeout=10,
    )
    try:
        if events.headers.get_content_type() != "text/event-stream":
            raise RuntimeError("SSE response has the wrong content type")
        if events.readline() != b": connected\n" or events.readline() != b"\n":
            raise RuntimeError("SSE proxy did not flush the connection prelude")
    finally:
        events.close()

    _json(
        opener, base_url, "/api/v1/operator/session",
        method="POST", body={"token": operator_token},
    )
    listing, _ = _json(opener, base_url, "/api/v1/operator/submissions")
    if not isinstance(listing, dict) or not isinstance(listing.get("submissions"), list):
        raise RuntimeError("Submission list response is invalid")
    submissions = len(listing["submissions"])
    _json(opener, base_url, "/api/v1/operator/session", method="DELETE")
    _json(opener, base_url, "/api/v1/session", method="DELETE")
    try:
        _json(opener, base_url, "/api/v1/session")
    except RuntimeError as error:
        if "HTTP 401" not in str(error):
            raise
    else:
        raise RuntimeError("Session remained authorized after logout")
    return SmokeResult(principal_id, replica_id, submissions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--access-code", required=True)
    parser.add_argument("--operator-token", required=True)
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--skip-static", action="store_true")
    arguments = parser.parse_args(argv)
    result = run(
        arguments.base_url,
        arguments.access_code,
        arguments.operator_token,
        allow_http=arguments.allow_http,
        check_static=not arguments.skip_static,
    )
    print(
        f"smoke passed: principal={result.principal_id} "
        f"replica={result.replica_id} submissions={result.submissions}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
