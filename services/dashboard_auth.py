"""Small signed-cookie session boundary for dashboard mutations."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from fastapi import Request

COOKIE = "veda_dashboard_session"
TTL = 12 * 60 * 60


def configured() -> bool:
    return bool(os.getenv("VEDA_DASHBOARD_PASSWORD") and os.getenv("VEDA_DASHBOARD_SESSION_SECRET"))


def credentials_valid(username: str, password: str) -> bool:
    expected_user = os.getenv("VEDA_DASHBOARD_USERNAME", "admin")
    expected_password = os.getenv("VEDA_DASHBOARD_PASSWORD", "")
    return bool(expected_password) and hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_password)


def create_session(username: str) -> tuple[str, str]:
    expiry = int(time.time()) + TTL
    csrf = secrets.token_urlsafe(24)
    value = f"{username}|{expiry}|{csrf}"
    signature = hmac.new(os.getenv("VEDA_DASHBOARD_SESSION_SECRET", "").encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}|{signature}", csrf


def read_session(request: Request) -> dict[str, str] | None:
    raw = request.cookies.get(COOKIE, "")
    parts = raw.split("|")
    if len(parts) != 4:
        return None
    username, expiry_raw, csrf, supplied = parts
    try:
        expiry = int(expiry_raw)
    except ValueError:
        return None
    value = f"{username}|{expiry}|{csrf}"
    expected = hmac.new(os.getenv("VEDA_DASHBOARD_SESSION_SECRET", "").encode(), value.encode(), hashlib.sha256).hexdigest()
    if expiry < time.time() or not hmac.compare_digest(supplied, expected):
        return None
    return {"username": username, "csrf": csrf}


def csrf_valid(request: Request, session: dict[str, str]) -> bool:
    return hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"])
