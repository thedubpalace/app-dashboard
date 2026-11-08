from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from services import settings_store

INDEX_HTML = Path(__file__).resolve().parent.parent / "templates" / "index.html"

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    # The page is fully client-rendered (data via /api/* fetches), so the
    # template has no server-side variables — serve it as static HTML. This
    # also sidesteps a Jinja2 template-cache bug on this Python/Jinja combo.
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@router.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return settings_store.load()


@router.patch("/api/settings/docker/filter")
async def set_docker_filter(payload: dict[str, Any]) -> JSONResponse:
    filters = payload.get("filter")
    if not isinstance(filters, list):
        return JSONResponse({"ok": False, "error": "filter must be a list"}, status_code=400)
    return JSONResponse({"ok": True, "filter": settings_store.set_docker_filter(filters)})
