"""Read the app registry and perform live health checks for each app."""
from __future__ import annotations

import json
import os
import shlex
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil

_DEFAULT_REGISTRY = str(Path(__file__).resolve().parents[2] / "veda" / ".veda" / "projects" / "registry.json")
REGISTRY_PATH = Path(os.getenv("APP_DASHBOARD_REGISTRY") or os.getenv("VEDA_REGISTRY", _DEFAULT_REGISTRY))

HEALTH_TIMEOUT = 3.0


def _load_registry() -> list[dict[str, Any]]:
    """Read registry.json fresh on every call (no caching)."""
    try:
        raw = REGISTRY_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    # Support both a bare list and a wrapper object {"projects": [...]}.
    if isinstance(data, dict):
        data = data.get("projects", [])
    if not isinstance(data, list):
        return []
    return data


def _first_cmd_token(run_cmd: str | None) -> str | None:
    if not run_cmd:
        return None
    try:
        tokens = shlex.split(run_cmd, posix=False)
    except ValueError:
        tokens = run_cmd.split()
    if not tokens:
        return None
    name = tokens[0].strip('"').strip("'")
    # Strip a path prefix and extension, e.g. C:/x/uvicorn.exe -> uvicorn
    name = Path(name).name
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def _check_port(target: Any) -> bool:
    """target may be a port number or a host:port / url string."""
    host = "localhost"
    port: int | None = None
    if isinstance(target, int):
        port = target
    elif isinstance(target, str):
        t = target.strip()
        if t.isdigit():
            port = int(t)
        elif "://" in t:
            parsed = urlparse(t)
            host = parsed.hostname or host
            port = parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)
        elif ":" in t:
            host, _, p = t.rpartition(":")
            host = host or "localhost"
            if p.isdigit():
                port = int(p)
    if port is None:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(HEALTH_TIMEOUT)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def snapshot_processes() -> list[dict[str, Any]]:
    """Scan the process list once; share the result across all health checks
    and session lookups in a request instead of re-scanning per app."""
    procs: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            procs.append(
                {
                    "pid": proc.pid,
                    "name": proc.info.get("name") or "",
                    "cmdline": proc.info.get("cmdline") or [],
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs


def _check_process(target: str | None, processes: list[dict[str, Any]]) -> bool:
    if not target:
        return False
    needle = Path(str(target)).name.lower()
    if needle.endswith(".exe"):
        needle = needle[:-4]
    for proc in processes:
        pname = (proc["name"] or "").lower()
        if pname.endswith(".exe"):
            pname = pname[:-4]
        if pname == needle:
            return True
        # For script targets (e.g. "agent.py"), match against cmdline args
        if any(Path(arg).name.lower() == needle for arg in proc["cmdline"]):
            return True
    return False


def _is_running(app: dict[str, Any], processes: list[dict[str, Any]]) -> bool:
    hc = app.get("healthCheck") or {}
    hc_type = (hc.get("type") or "null").lower()
    target = hc.get("target")

    if hc_type == "none":
        # Explicitly not a runnable local service (e.g. huawei-watchface) —
        # never health-checked, never reported as running.
        return False
    if hc_type == "http":
        # A raw TCP connect is enough to tell "running" from "stopped" here,
        # and it's far more reliably bounded by its timeout than an HTTP GET
        # (httpx.get() was observed hanging well past its timeout= on this
        # host, likely due to AV/EDR intercepting outbound HTTP traffic).
        return _check_port(target)
    if hc_type == "port":
        return _check_port(target)
    if hc_type == "process":
        return _check_process(target, processes)
    # null / unknown: fall back to first token of runCmd as a process name
    return _check_process(_first_cmd_token(app.get("runCmd")), processes)


def _is_monitorable(app: dict[str, Any]) -> bool:
    """Return True only when the app can be started/stopped/restarted and
    should show a live status indicator on the dashboard.

    Apps with healthCheck.type == "none" (e.g. huawei-watchface — not a
    runnable local service) are always excluded, even if a runCmd exists.
    """
    hc = app.get("healthCheck") or {}
    if (hc.get("type") or "").lower() == "none":
        return False
    return bool(app.get("runCmd"))


def list_apps(processes: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return registry apps enriched with live `status` and `monitorable` fields.

    Health checks (http/port/process) run concurrently since they're blocking
    I/O — sequential timeouts on multiple down apps used to add up directly.
    """
    registry = _load_registry()
    if processes is None:
        processes = snapshot_processes()
    monitorable_flags = [_is_monitorable(app) for app in registry]

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(registry)))) as ex:
        running_flags = list(
            ex.map(
                lambda pair: _is_running(pair[0], processes) if pair[1] else False,
                zip(registry, monitorable_flags),
            )
        )

    apps: list[dict[str, Any]] = []
    for app, monitorable, running in zip(registry, monitorable_flags, running_flags):
        apps.append(
            {
                "name": app.get("name"),
                "description": app.get("description"),
                "status": "running" if running else "stopped",
                "monitorable": monitorable,
                "runCmd": app.get("runCmd"),
                "repo": app.get("repo"),
                "localPath": app.get("localPath"),
                "healthCheck": app.get("healthCheck"),
                "containerName": app.get("containerName"),
                "dockerImage": app.get("dockerImage"),
            }
        )
    return apps


def get_app(name: str) -> dict[str, Any] | None:
    for app in _load_registry():
        if app.get("name") == name:
            return app
    return None
