from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services import app_links, docker_service, settings_store, system_service

router = APIRouter(prefix="/api/docker")


def _docker_with_filter() -> dict[str, Any]:
    result = docker_service.apply_filter(docker_service.list_containers(), settings_store.docker_filter())
    if isinstance(result, dict) and "containers" in result:
        app_links.attach_container_urls(result["containers"])
    return result


@router.get("")
async def docker_list() -> dict[str, Any]:
    return await asyncio.to_thread(_docker_with_filter)


@router.post("/fix")
async def docker_fix() -> JSONResponse:
    result = system_service.fix_docker_desktop()
    return JSONResponse(result, status_code=200 if result["ok"] else 500)


@router.post("/{name}/start")
async def docker_start(name: str) -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(docker_service.start, name))


@router.post("/{name}/stop")
async def docker_stop(name: str) -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(docker_service.stop, name))


@router.post("/{name}/restart")
async def docker_restart(name: str) -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(docker_service.restart, name))
