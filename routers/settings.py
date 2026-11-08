from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services import settings_store

router = APIRouter(prefix="/api/settings")


@router.get("")
async def get_settings() -> dict[str, Any]:
    return settings_store.load()


@router.patch("/docker/filter")
async def set_docker_filter(payload: dict[str, Any]) -> JSONResponse:
    filters = payload.get("filter")
    if not isinstance(filters, list):
        return JSONResponse({"ok": False, "error": "filter must be a list"}, status_code=400)
    return JSONResponse({"ok": True, "filter": settings_store.set_docker_filter(filters)})
