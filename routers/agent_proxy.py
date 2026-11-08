"""Relay /api requests from the dashboard UI to the host agent.

The browser never sees the agent token: it is added here, server side.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from fastapi import APIRouter, Request, Response

# Longer than the slowest agent action (a Compose start can take 180 s).
TIMEOUT = 240
_FORWARDED_HEADERS = ("content-type", "accept")


def _send(url: str, method: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes, str]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(), resp.headers.get("content-type", "application/json")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("content-type", "application/json")


def build_router(agent_url: str, token: str | None) -> APIRouter:
    base = agent_url.rstrip("/")
    router = APIRouter()

    @router.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def relay(path: str, request: Request) -> Response:
        url = f"{base}/api/{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        headers = {k: v for k, v in request.headers.items() if k in _FORWARDED_HEADERS}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = await request.body() or None
        try:
            status, content, content_type = await asyncio.to_thread(_send, url, request.method, body, headers)
        except (OSError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", exc)
            content = json.dumps({"ok": False, "agentOffline": True, "error": f"Host agent unreachable at {base}: {reason}"})
            return Response(content, status_code=502, media_type="application/json")
        if status == 401:
            content = json.dumps({"ok": False, "error": "Host agent rejected the token. Check APP_AGENT_TOKEN."}).encode()
        return Response(content, status_code=status, media_type=content_type)

    return router
