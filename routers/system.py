from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services import system_service

router = APIRouter()


@router.post("/api/system/sunshine/restart")
async def sunshine_restart() -> JSONResponse:
    result = await asyncio.to_thread(system_service.restart_sunshine)
    return JSONResponse(result, status_code=200 if result["ok"] else 503)


@router.post("/api/system/restart")
async def system_restart() -> JSONResponse:
    try:
        system_service.restart_machine()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "message": "Machine restarting in 10 seconds…"})


@router.get("/api/tailscale/status")
async def tailscale_status() -> dict[str, Any]:
    return await asyncio.to_thread(system_service.tailscale_status)


@router.post("/api/tailscale/toggle")
async def tailscale_toggle() -> JSONResponse:
    status = await asyncio.to_thread(system_service.toggle_tailscale)
    return JSONResponse({"connected": status["connected"], "ip": status["ip"]})
