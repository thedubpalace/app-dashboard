"""HTTP middleware that enforces services/access_policy.py on every request.

Order of checks for a request:
1. Static files and PUBLIC rules pass.
2. No rule for (method, path) -> 403 ``not_allowed`` (default deny).
3. No valid signed session -> 401 (``session_expired`` if a cookie was sent).
4. Mutation: foreign ``Origin`` -> 403 ``origin_mismatch``; missing/wrong
   ``X-CSRF-Token`` -> 403 ``csrf_invalid``.
5. Role lacks the rule's permission -> 403 ``forbidden``.
6. Mutation: write the ``requested`` audit record (failure -> 503, nothing
   runs), call the route, write the ``result`` record.
"""
from __future__ import annotations

import os
import uuid
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from services import access_policy, audit_log
from services.dashboard_auth import COOKIE, csrf_valid, read_session, role_for, session_fingerprint

NO_STORE = {"Cache-Control": "no-store"}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), usb=()",
    "Content-Security-Policy": (
        "default-src 'self' https://fonts.googleapis.com https://fonts.gstatic.com; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}

MESSAGES = {
    "unauthenticated": "Sign in first.",
    "session_expired": "Your session expired. Sign in again.",
    "csrf_invalid": "Invalid CSRF token. Reload the page and try again.",
    "origin_mismatch": "Cross-origin request refused.",
    "forbidden": "Your role is not allowed to do this.",
    "not_allowed": "This route is not in the dashboard access policy.",
}


def _client(request: Request) -> str | None:
    return request.client.host if request.client else None


def _surface(path: str) -> str:
    return "review" if path == "/review" or path.startswith("/api/review/") else "dashboard"


def _deny(request: Request, status: int, code: str, permission: str | None, username: str | None = None) -> JSONResponse:
    audit_log.security({
        "surface": _surface(request.url.path), "permission": permission, "method": request.method, "path": request.url.path,
        "status": status, "code": code, "username": username, "client": _client(request),
    })
    return JSONResponse({"ok": False, "code": code, "error": MESSAGES[code]}, status_code=status, headers=NO_STORE)


def _allowed_origins(request: Request) -> set[str]:
    hosts = {request.headers.get("host", "")}
    forwarded = request.headers.get("x-forwarded-host", "")
    hosts.update(part.strip() for part in forwarded.split(",") if part.strip())
    hosts.update(item.strip() for item in os.getenv("DASHBOARD_ALLOWED_ORIGINS", "").split(",") if item.strip())
    return {host.lower() for host in hosts if host}


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True  # same-origin fetches from older browsers, CLI clients; CSRF token still required
    netloc = urlparse(origin).netloc.lower()
    return bool(netloc) and netloc in _allowed_origins(request)


async def _guard(request: Request, call_next):
    method, path = request.method.upper(), request.url.path
    if access_policy.is_static(method, path):
        return await call_next(request)
    rule = access_policy.match(method, path)
    if rule is None:
        return _deny(request, 403, "not_allowed", None)
    if rule.permission == access_policy.PUBLIC:
        return await call_next(request)

    session = read_session(request)
    if not session:
        return _deny(request, 401, "session_expired" if request.cookies.get(COOKIE) else "unauthenticated", rule.permission)
    username = session["username"]
    if rule.mutation:
        if not _origin_ok(request):
            return _deny(request, 403, "origin_mismatch", rule.permission, username)
        if not csrf_valid(request, session):
            return _deny(request, 403, "csrf_invalid", rule.permission, username)
    role = role_for(username)
    if not access_policy.allowed(role, rule.permission):
        return _deny(request, 403, "forbidden", rule.permission, username)
    if not rule.mutation:
        return await call_next(request)

    record = {
        "id": uuid.uuid4().hex, "actor": username, "role": role, "method": method, "path": path,
        "permission": rule.permission, "client": _client(request), "session": session_fingerprint(request),
    }
    try:
        audit_log.audit({**record, "phase": "requested"})
    except OSError:
        return JSONResponse({"ok": False, "code": "audit_unavailable", "error": "Audit log is unavailable; nothing was changed."},
                            status_code=503, headers=NO_STORE)
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        try:
            audit_log.audit({**record, "phase": "result", "status": status})
        except OSError:
            pass


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def access_and_headers(request: Request, call_next):
        response = await _guard(request, call_next)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
