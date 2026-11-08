"""Start, stop, restart and switch the mode of registered apps.

Each operation returns ``(payload, status_code)`` so a caller can relay it
over HTTP unchanged. All calls block; run them off the event loop.
"""
from __future__ import annotations

import shlex
import subprocess
from typing import Any

from services import app_modes, docker_service, local_process, settings_store
from services.process_window import NO_WINDOW, hidden_startupinfo

Result = tuple[dict[str, Any], int]


def start_local(entry: dict[str, Any]) -> str | None:
    """Launch the app's dev command on this host. Returns an error, or None."""
    run_cmd = app_modes.dev_cmd(entry)
    if not run_cmd:
        return "no dev command configured"
    return local_process.spawn_hidden(run_cmd, entry.get("localPath"))


def stop_local(entry: dict[str, Any]) -> int:
    """Kill the host processes serving this app. Returns how many were killed."""
    port = app_modes.dev_port(entry)

    killed = local_process.kill_by_port(port) if port is not None else 0
    if killed == 0:
        # Fall back: match the first command token against process names.
        token = app_modes.first_token(app_modes.dev_cmd(entry))
        if token and entry.get("localPath"):
            killed += local_process.kill_by_name(token, entry["localPath"])
    return killed


def start_container(entry: dict[str, Any], container: str) -> dict[str, Any]:
    """Bring up configured dependencies before starting a Compose-backed app."""
    command = app_modes.app_settings(entry).get("dockerStartCmd")
    if not command:
        return docker_service.start(container)
    try:
        result = subprocess.run(
            shlex.split(command, posix=False),
            cwd=entry.get("localPath"),
            capture_output=True,
            text=True,
            timeout=180,
            startupinfo=hidden_startupinfo(), creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    if result.returncode:
        return {"ok": False, "error": (result.stderr or result.stdout).strip()}
    return {"ok": True, "message": f"started {container} and its dependencies"}


def start(entry: dict[str, Any]) -> Result:
    name = entry.get("name")
    container = app_modes.container_for(entry)
    if container and app_modes.app_mode(entry) == "docker":
        result = start_container(entry, container)
        if result.get("ok"):
            result.setdefault("message", f"started container {container}")
        return result, 200

    if not app_modes.dev_cmd(entry):
        return {"ok": False, "error": "no dev command configured"}, 400

    error = start_local(entry)
    if error:
        return {"ok": False, "error": error}, 500
    return {"ok": True, "message": f"started {name}"}, 200


def stop(entry: dict[str, Any]) -> Result:
    name = entry.get("name")
    container = app_modes.container_for(entry)
    if container and app_modes.app_mode(entry) == "docker":
        result = docker_service.stop(container)
        if result.get("ok"):
            result.setdefault("message", f"stopped container {container}")
        return result, 200

    killed = stop_local(entry)
    if killed:
        return {"ok": True, "message": f"stopped {name} ({killed} proc)"}, 200
    return {"ok": False, "error": "no matching process found"}, 404


def restart(entry: dict[str, Any]) -> Result:
    name = entry.get("name")
    # Container-backed apps (dockerImage/containerName in the registry) are
    # restarted via `docker restart` — never by killing/re-running runCmd,
    # since runCmd for these entries is typically just the local-dev fallback.
    container = app_modes.container_for(entry)
    if container and app_modes.app_mode(entry) == "docker":
        if app_modes.app_settings(entry).get("dockerStartCmd"):
            started = start_container(entry, container)
            if not started.get("ok"):
                return started, 500
        result = docker_service.restart(container)
        if result.get("ok"):
            result.setdefault("message", f"restarted container {container}")
        return result, 200

    run_cmd = app_modes.dev_cmd(entry)
    if not run_cmd:
        return {"ok": False, "error": "no dev command configured"}, 400

    port = app_modes.dev_port(entry)
    token = None if port else app_modes.first_token(run_cmd)
    try:
        local_process.spawn_restart(port, token, run_cmd, entry.get("localPath") or None)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}, 500
    return {"ok": True, "message": f"restarting {name}…"}, 200


def set_mode(entry: dict[str, Any], mode: str) -> Result:
    """Flip an app between its dev server and its container, stopping whichever
    side is losing so the two never fight over the same port."""
    name = entry.get("name")
    if mode not in ("dev", "docker"):
        return {"ok": False, "error": "mode must be 'dev' or 'docker'"}, 400
    if not app_modes.supports_modes(entry):
        return {"ok": False, "error": f"{name} has no dev/docker choice to make"}, 400

    container = app_modes.container_for(entry)
    settings_store.set_app_mode(name, mode)

    if mode == "docker":
        stop_local(entry)
        result = start_container(entry, container)
        if not result.get("ok"):
            return {"ok": False, "error": f"docker start {container}: {result.get('error')}"}, 500
        return {"ok": True, "message": f"{name} now running in Docker"}, 200

    # dev: free the port the container holds before the dev server binds it.
    docker_service.stop(container)
    error = start_local(entry)
    if error:
        return {"ok": False, "error": error}, 500
    return {"ok": True, "message": f"{name} now running as dev server"}, 200
