"""App Dashboard host agent — owns every action on this Windows host.

Run: uvicorn agent:app --host 127.0.0.1 --port 8766   (or start-agent.ps1)

Serves the /api routes the dashboard UI uses, behind a bearer token, and runs
the reconciler that keeps desired VMs and apps up. It must run natively on the
host in the user's session: it launches dev servers, opens terminals, and
drives vmrun, Docker Desktop, Sunshine and Tailscale.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import Depends, FastAPI, Header, HTTPException

from routers import API_ROUTERS
from services import agent_token, reconciler

TOKEN = agent_token.load_or_create()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def require_token(authorization: str | None = Header(default=None)) -> None:
    if not agent_token.matches(TOKEN, authorization):
        raise HTTPException(status_code=401, detail="missing or invalid agent token")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(reconciler.run_forever())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="App Dashboard Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
for router in API_ROUTERS:
    app.include_router(router, dependencies=[Depends(require_token)])


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
