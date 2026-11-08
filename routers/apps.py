from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services import agent_sessions, app_control, app_links, app_modes, app_registry, settings_store

router = APIRouter(prefix="/api/apps")


def _unknown(name: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": f"unknown app: {name}"}, status_code=404)


def _apps_with_sessions() -> list[dict[str, Any]]:
    processes = app_registry.snapshot_processes()
    result = app_registry.list_apps(processes)
    agent_sessions.attach_session_counts(result, processes)
    app_modes.attach_run_modes(result)
    app_links.attach_public_urls(result)
    return result


async def _control(
    name: str, action: Callable[..., app_control.Result], *args: Any, desired_running: bool | None = None
) -> JSONResponse:
    entry = app_registry.get_app(name)
    if not entry:
        return _unknown(name)
    payload, status_code = await asyncio.to_thread(action, entry, *args)
    if desired_running is not None and payload.get("ok"):
        # A dashboard stop is intentional, so the reconciler must not undo it;
        # starting again re-enables auto-recovery. Mirrors VMware.
        settings_store.update_app_desired_running(name, desired_running)
    return JSONResponse(payload, status_code=status_code)


@router.get("")
async def apps() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_apps_with_sessions)


@router.post("/{name}/start")
async def app_start(name: str) -> JSONResponse:
    return await _control(name, app_control.start, desired_running=True)


@router.post("/{name}/stop")
async def app_stop(name: str) -> JSONResponse:
    return await _control(name, app_control.stop, desired_running=False)


@router.post("/{name}/restart")
async def app_restart(name: str) -> JSONResponse:
    return await _control(name, app_control.restart, desired_running=True)


@router.post("/{name}/mode")
async def app_set_mode(name: str, payload: dict[str, Any]) -> JSONResponse:
    return await _control(name, app_control.set_mode, str(payload.get("mode") or "").lower())


async def _open_terminal(
    name: str,
    find: Callable[[], str | None],
    launch: Callable[[str, str], None],
    missing: str,
    opened: str,
) -> JSONResponse:
    entry = app_registry.get_app(name)
    if not entry:
        return _unknown(name)
    cwd = entry.get("localPath")
    if not cwd or not Path(cwd).is_dir():
        return JSONResponse({"ok": False, "error": "localPath not found"}, status_code=400)
    exe = find()
    if not exe:
        return JSONResponse({"ok": False, "error": missing}, status_code=503)
    try:
        launch(cwd, exe)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "message": f"{opened} for {name}"})


@router.post("/{name}/shell")
async def app_shell(name: str) -> JSONResponse:
    return await _open_terminal(
        name, agent_sessions.find_claude_executable, agent_sessions.open_claude,
        "Claude Code CLI was not found. Install it or add it to PATH.", "opened shell",
    )


@router.post("/{name}/codex")
async def app_codex(name: str) -> JSONResponse:
    """Open an interactive Codex session in an app's local folder."""
    return await _open_terminal(
        name, agent_sessions.find_codex_executable, agent_sessions.open_codex,
        "Codex CLI was not found. Install or update the Codex desktop app.", "opened Codex",
    )


@router.post("/{name}/claude-sessions/kill-all")
async def kill_app_claude_sessions(name: str) -> JSONResponse:
    entry = app_registry.get_app(name)
    if not entry:
        return _unknown(name)
    killed = await asyncio.to_thread(agent_sessions.kill_claude_sessions, entry.get("localPath"))
    msg = f"terminated {killed} session(s)" if killed else "no sessions found"
    return JSONResponse({"ok": True, "killed": killed, "message": msg})


@router.post("/{name}/codex-sessions/kill-all")
async def kill_app_codex_sessions(name: str) -> JSONResponse:
    entry = app_registry.get_app(name)
    if not entry:
        return _unknown(name)
    killed = await asyncio.to_thread(agent_sessions.kill_codex_sessions, entry.get("localPath"))
    msg = f"terminated {killed} Codex session(s)" if killed else "no Codex sessions found"
    return JSONResponse({"ok": True, "killed": killed, "message": msg})
