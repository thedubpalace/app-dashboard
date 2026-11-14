"""Sign in / sign out for the dashboard UI (served by main.py only, never by the agent)."""
from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services import audit_log
from services.dashboard_auth import COOKIE, TTL, configured, create_session, credentials_valid, read_session, role_for

router = APIRouter()
_login_failures: dict[str, list[float]] = {}


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _secure_cookie() -> bool:
    return os.getenv("DASHBOARD_COOKIE_SECURE", "0") == "1"


@router.post("/api/login")
async def login(request: Request, payload: dict[str, Any]) -> JSONResponse:
    if not configured():
        return JSONResponse({"ok": False, "error": "dashboard authentication is not configured"}, status_code=503)
    username, password = str(payload.get("username", "")), str(payload.get("password", ""))
    window = int(os.getenv("DASHBOARD_LOGIN_RATE_WINDOW_SECONDS", "900"))
    maximum = int(os.getenv("DASHBOARD_LOGIN_RATE_ATTEMPTS", "5"))
    key = f"{_client(request)}:{username.lower()}"
    now = time.time()
    attempts = [value for value in _login_failures.get(key, []) if now - value < window]
    if len(attempts) >= maximum:
        audit_log.security({"surface": "login", "method": "POST", "path": "/api/login", "status": 429, "code": "rate_limited",
                            "username": username[:64], "client": _client(request)})
        return JSONResponse({"ok": False, "error": "too many login attempts"}, status_code=429, headers={"Retry-After": str(window)})
    if not credentials_valid(username, password):
        _login_failures[key] = [*attempts, now]
        audit_log.security({"surface": "login", "method": "POST", "path": "/api/login", "status": 401, "code": "invalid_credentials",
                            "username": username[:64], "client": _client(request)})
        return JSONResponse({"ok": False, "error": "invalid credentials"}, status_code=401)
    _login_failures.pop(key, None)
    value, csrf_token = create_session(username)
    try:
        audit_log.audit({"actor": username, "role": role_for(username), "method": "POST", "path": "/api/login", "phase": "result",
                         "status": 200, "client": _client(request)})
    except OSError:
        pass
    response = JSONResponse({"ok": True, "username": username, "role": role_for(username), "csrfToken": csrf_token})
    response.set_cookie(COOKIE, value, httponly=True, secure=_secure_cookie(), samesite="strict", max_age=TTL)
    return response


@router.get("/api/session")
async def session(request: Request) -> JSONResponse:
    value = read_session(request)
    body = {
        "authenticated": bool(value),
        "username": value.get("username") if value else None,
        "role": role_for(value["username"]) if value else None,
        "csrfToken": value.get("csrf") if value else None,
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@router.post("/api/logout")
async def logout(request: Request) -> JSONResponse:
    value = read_session(request)  # the access guard already required session + CSRF
    response = JSONResponse({"ok": True, "username": value["username"] if value else None})
    response.delete_cookie(COOKIE, httponly=True, secure=_secure_cookie(), samesite="strict")
    return response
