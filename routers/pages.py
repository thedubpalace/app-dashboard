from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

INDEX_HTML = Path(__file__).resolve().parent.parent / "templates" / "index.html"

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    # The page is fully client-rendered (data via /api/* fetches), so the
    # template has no server-side variables — serve it as static HTML. This
    # also sidesteps a Jinja2 template-cache bug on this Python/Jinja combo.
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))
