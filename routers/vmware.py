from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services import settings_store, vmware_service

router = APIRouter(prefix="/api/vmware")


@router.get("")
async def vmware_list() -> dict[str, Any]:
    return await asyncio.to_thread(vmware_service.list_vms, settings_store.managed_vms())


@router.post("/start")
async def vmware_start(payload: dict[str, Any]) -> JSONResponse:
    vmx = payload.get("vmx")
    if not vmx:
        return JSONResponse({"ok": False, "error": "vmx required"}, status_code=400)
    result = await asyncio.to_thread(vmware_service.start_vm, vmx, bool(payload.get("gui", False)))
    if result.get("ok"):
        settings_store.set_vm_desired_running(vmx, True)
    return JSONResponse(result)


@router.post("/stop")
async def vmware_stop(payload: dict[str, Any]) -> JSONResponse:
    vmx = payload.get("vmx")
    if not vmx:
        return JSONResponse({"ok": False, "error": "vmx required"}, status_code=400)
    # A dashboard-initiated stop is intentional, so do not immediately undo it
    # with the watchdog. Starting it again re-enables auto-recovery.
    settings_store.set_vm_desired_running(vmx, False)
    return JSONResponse(await asyncio.to_thread(vmware_service.stop_vm, vmx, bool(payload.get("force", False))))
