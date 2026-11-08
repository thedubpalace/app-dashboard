"""Shared secret between the dashboard UI and the host agent.

APP_AGENT_TOKEN wins when set (pass it to the UI container this way). Otherwise
the agent creates .agent-token next to this repo on first start, and a UI
running natively on the same host reads it from there.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

TOKEN_FILE = Path(__file__).resolve().parent.parent / ".agent-token"


def load() -> str | None:
    token = os.environ.get("APP_AGENT_TOKEN", "").strip()
    if token:
        return token
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def load_or_create() -> str:
    token = load()
    if token:
        return token
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


def matches(expected: str, authorization: str | None) -> bool:
    scheme, _, supplied = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and secrets.compare_digest(supplied.strip(), expected)
