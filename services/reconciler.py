"""Keep the host in the state settings.json asks for.

VMs marked ``desiredRunning`` are kept running (Task Scheduler independently
covers reboot). Apps opt in the same way with ``"desiredRunning": true`` under
settings.json ``apps``; the dashboard's start/stop buttons then keep it in step
so an intentional stop is never undone.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from services import app_control, app_modes, app_registry, settings_store, vmware_service

INITIAL_DELAY = 30
INTERVAL = 60
# A failing app is retried at most this often, so a broken start command
# does not spawn a new process every cycle.
APP_RETRY_COOLDOWN = 300

log = logging.getLogger("app_dashboard.reconciler")
_last_attempt: dict[str, float] = {}


def desired_apps(settings: dict[str, Any] | None = None) -> set[str]:
    cfg = (settings if settings is not None else settings_store.load()).get("apps", {})
    if not isinstance(cfg, dict):
        return set()
    return {name for name, app in cfg.items() if isinstance(app, dict) and app.get("desiredRunning") is True}


def reconcile_apps(now: float | None = None) -> list[str]:
    """Start desired apps that are down. Returns the names it tried to start."""
    wanted = desired_apps()
    if not wanted:
        return []
    apps = [a for a in app_registry.list_apps() if a.get("name") in wanted]
    app_modes.attach_run_modes(apps)
    now = time.monotonic() if now is None else now
    attempted = []
    for app in apps:
        name = app["name"]
        if app.get("status") == "running" or not app.get("monitorable"):
            continue
        if now - _last_attempt.get(name, float("-inf")) < APP_RETRY_COOLDOWN:
            continue
        entry = app_registry.get_app(name)
        if not entry:
            continue
        _last_attempt[name] = now
        attempted.append(name)
        payload, status_code = app_control.start(entry)
        if payload.get("ok"):
            log.info("started %s: %s", name, payload.get("message"))
        else:
            log.warning("could not start %s (%s): %s", name, status_code, payload.get("error"))
    return attempted


def reconcile_once() -> None:
    vmware_service.ensure_desired_running(settings_store.managed_vms())
    reconcile_apps()


async def run_forever() -> None:
    await asyncio.sleep(INITIAL_DELAY)
    while True:
        try:
            await asyncio.to_thread(reconcile_once)
        except Exception:  # noqa: BLE001
            log.exception("reconcile cycle failed")
        await asyncio.sleep(INTERVAL)
