"""Control-plane readiness (public health probe, booleans only).

Used by monitor.ps1, the watchdog's operators and the Temporal orchestrator's
``service.app-dashboard`` health policy. It always answers 200 so a probe can
tell "dashboard up" from "Temporal degraded"; ``ok`` is the fail-closed verdict.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from services.temporal_client import TemporalClientError, client

router = APIRouter()


def _snapshot() -> dict[str, Any]:
    try:
        return client.resources(actor="dashboard-read", role="operator")
    except TemporalClientError as exc:
        return {"resources": [], "registrySync": {"status": "unavailable", "error": str(exc)}}


def readiness_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    sync, dependencies = snapshot.get("registrySync") or {}, snapshot.get("readiness") or {}
    ok = sync.get("status") == "ready" and dependencies.get("temporal") is True and dependencies.get("worker") is True
    if sync.get("status") != "ready":
        reason = str(sync.get("error") or "Registry synchronization is degraded")
    elif dependencies.get("worker") is not True:
        reason = "Required worker task queue is offline"
    elif dependencies.get("temporal") is not True:
        reason = "Temporal control plane is unavailable"
    else:
        reason = ""
    return {
        "ok": ok,
        "dashboard": True,
        "status": sync.get("status"),
        "resourceCount": sync.get("resourceCount"),
        "syncedAt": sync.get("syncedAt"),
        "dependencies": {key: value for key, value in dependencies.items() if isinstance(value, bool)},
        "error": reason,
    }


@router.get("/api/control/readiness")
async def readiness() -> dict[str, Any]:
    return readiness_payload(await asyncio.to_thread(_snapshot))
