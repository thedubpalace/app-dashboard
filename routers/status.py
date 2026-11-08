from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from services import agent_sessions, app_registry, docker_service, settings_store, vmware_service

router = APIRouter()


@router.get("/api/status")
async def status() -> dict[str, Any]:
    processes = await asyncio.to_thread(app_registry.snapshot_processes)
    apps, docker, vmware = await asyncio.gather(
        asyncio.to_thread(app_registry.list_apps, processes),
        asyncio.to_thread(docker_service.list_containers),
        asyncio.to_thread(vmware_service.list_vms, settings_store.managed_vms()),
    )
    agent_sessions.attach_session_counts(apps, processes)
    docker = docker_service.apply_filter(docker, settings_store.docker_filter())
    return {"apps": apps, "docker": docker, "vmware": vmware}
