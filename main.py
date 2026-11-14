"""App Dashboard — unified control panel for registered apps, Docker, and VMware.

Run: uvicorn main:app --reload --port 8765

Two ways to run it:

- All-in-one (default): this process serves the page *and* the /api routes,
  acting on the host itself.
- UI only: set APP_AGENT_URL (e.g. http://127.0.0.1:8766). This process then
  serves the page and relays /api to the host agent (agent.py), so it can run
  anywhere — including a Docker container.

Routes live in routers/ and only translate HTTP; everything that touches the
host (processes, Docker, VMware, Windows services) lives in services/.

Access: services/access_guard.py checks every request against the table in
services/access_policy.py (signed session, CSRF on mutations, role permission,
audit record; anything not in the table is denied). Sign-in, readiness and the
requirement review are served here, never relayed to the agent.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from routers import API_ROUTERS, agent_proxy, auth, control, pages
from services import access_guard, agent_token, reconciler
from services.review_api import router as review_router

BASE_DIR = Path(__file__).resolve().parent
AGENT_URL = os.environ.get("APP_AGENT_URL", "").strip() or None


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    # With an agent, keeping the host in its desired state is the agent's job.
    # APP_DASHBOARD_RECONCILER=0 turns it off (staging copies next to a live dashboard).
    enabled = not AGENT_URL and os.environ.get("APP_DASHBOARD_RECONCILER", "1") != "0"
    task = asyncio.create_task(reconciler.run_forever()) if enabled else None
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(title="App Dashboard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
access_guard.install(app)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(pages.router)
# Before the agent relay's /api/{path:path} catch-all, so these are never relayed.
app.include_router(auth.router)
app.include_router(control.router)
app.include_router(review_router)
if AGENT_URL:
    app.include_router(agent_proxy.build_router(AGENT_URL, agent_token.load()))
else:
    for router in API_ROUTERS:
        app.include_router(router)
