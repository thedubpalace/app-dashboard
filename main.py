"""App Dashboard — unified control panel for registered apps, Docker, and VMware.

Run: uvicorn main:app --reload --port 8765

Routes live in routers/ and only translate HTTP; everything that touches the
host (processes, Docker, VMware, Windows services) lives in services/.
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from routers import apps, docker, pages, status, system, vmware

BASE_DIR = Path(__file__).resolve().parent


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    watchdog = asyncio.create_task(vmware.watchdog())
    try:
        yield
    finally:
        watchdog.cancel()


app = FastAPI(title="App Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
for module in (pages, status, apps, docker, system, vmware):
    app.include_router(module.router)
