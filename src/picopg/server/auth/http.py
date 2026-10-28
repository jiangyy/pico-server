from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from picopg.server.replication import ResponseSent, logger
from picopg.replica import InvalidReplicaData

if TYPE_CHECKING:
    from picopg.server.app import PicoRequestHandler


def dispatch_session(handler: PicoRequestHandler, path: str) -> bool:
    if path not in ("/api/v1/session", "/api/v1/operator/session"):
        return False
    try:
        if path == "/api/v1/session":
            _student_session(handler)
        else:
            _operator_session(handler)
    except (InvalidReplicaData, ValueError) as error:
        handler.error(400, "session.invalid", str(error))
    except ResponseSent:
        pass
    except Exception:
        logger.exception("Session request failed")
        handler.error(500, "server.failure", "server failure")
    return True


def _student_session(handler: PicoRequestHandler) -> None:
    registry = handler.server.session_registry
    if registry is None:
        handler.error(404, "route.not_found", "route not found")
        return
    if handler.command == "GET":
        identity = handler.authenticated_replica()
        if identity is None:
            handler.error(401, "auth.required", "valid Session required")
            return
        handler.send_json(200, {
            "principalId": identity.principal_id,
            "replicaId": identity.replica_id,
        }, (("Cache-Control", "no-store"),))
        return
    if handler.command == "POST":
        # Native clients (Electron) keep tokens in OS-encrypted storage instead of a cookie jar.
        native = handler.headers.get("X-Pico-Client") == "native"
        body = handler.read_json()
        allowed = {"accessCode", "deviceToken"} if native else {"accessCode"}
        if not isinstance(body, dict) or "accessCode" not in body or not set(body) <= allowed:
            handler.error(400, "session.invalid", "body must contain accessCode")
            return
        access_code = body["accessCode"]
        if not isinstance(access_code, str) or not access_code:
            handler.error(400, "session.invalid", "accessCode must be non-empty")
            return
        account = next((
            candidate
            for candidate in handler.server.principal_accounts
            if hmac.compare_digest(access_code, candidate.access_code)
        ), None)
        if account is None:
            handler.error(401, "auth.required", "valid access code required")
            return
        device = body.get("deviceToken") if native else handler.cookie("picopg_device")
        if device is not None and (not isinstance(device, str) or not device):
            handler.error(400, "session.invalid", "deviceToken must be non-empty")
            return
        session = registry.bootstrap(account.principal_id, device)
        if native:
            handler.send_json(201, {
                "principalId": session.identity.principal_id,
                "replicaId": session.identity.replica_id,
                "sessionToken": session.session_token,
                "deviceToken": session.device_token,
                "expiresAt": session.expires_at,
            }, (("Cache-Control", "no-store"),))
            return
        handler.send_json(201, {
            "principalId": session.identity.principal_id,
            "replicaId": session.identity.replica_id,
        }, (
            ("Set-Cookie", _cookie(handler, "picopg_session", session.session_token, registry.ttl_seconds)),
            ("Set-Cookie", _cookie(handler, "picopg_device", session.device_token, 365 * 24 * 60 * 60)),
            ("Cache-Control", "no-store"),
        ))
        return
    if handler.command == "DELETE":
        token = handler._bearer_token() or handler.cookie("picopg_session")
        if token is not None:
            registry.revoke(token)
        handler.send_json(200, {"status": "signedOut"}, (
            ("Set-Cookie", _cookie(handler, "picopg_session", "", 0)),
            ("Cache-Control", "no-store"),
        ))
        return
    handler.error(405, "method.not_allowed", "method not allowed")


def _operator_session(handler: PicoRequestHandler) -> None:
    registry = handler.server.session_registry
    if registry is None:
        handler.error(404, "route.not_found", "route not found")
        return
    if handler.command == "POST":
        body = handler.read_json()
        if not isinstance(body, dict) or set(body) != {"token"}:
            handler.error(400, "session.invalid", "body must contain token")
            return
        token = body["token"]
        if not isinstance(token, str) or not hmac.compare_digest(token, handler.server.replica_token):
            handler.error(401, "auth.required", "valid operator token required")
            return
        session = registry.create_operator()
        handler.send_json(201, {"status": "authenticated"}, (
            ("Set-Cookie", _cookie(handler, "picopg_operator_session", session.token, registry.ttl_seconds)),
            ("Cache-Control", "no-store"),
        ))
        return
    if handler.command == "DELETE":
        token = handler.cookie("picopg_operator_session")
        if token is not None:
            registry.revoke_operator(token)
        handler.send_json(200, {"status": "signedOut"}, (
            ("Set-Cookie", _cookie(handler, "picopg_operator_session", "", 0)),
            ("Cache-Control", "no-store"),
        ))
        return
    handler.error(405, "method.not_allowed", "method not allowed")


def _cookie(handler: PicoRequestHandler, name: str, value: str, max_age: int) -> str:
    parts = [f"{name}={value}", "Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Strict"]
    if handler.server.secure_cookies:
        parts.append("Secure")
    return "; ".join(parts)
