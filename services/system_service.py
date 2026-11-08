"""Host-level controls: Docker Desktop recovery, Sunshine, Tailscale, machine restart."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from services.process_window import NO_WINDOW, hidden_startupinfo

DOCKER_DESKTOP = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"


def _run_hidden(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, startupinfo=hidden_startupinfo(), creationflags=NO_WINDOW, **kwargs)


def fix_docker_desktop() -> dict[str, Any]:
    """Kill Docker Desktop and WSL, then relaunch it in the background."""

    def _restart():
        # Kill all Docker Desktop processes
        _run_hidden(["taskkill", "/F", "/IM", "Docker Desktop.exe"])
        _run_hidden(["taskkill", "/F", "/IM", "com.docker.backend.exe"])
        # Reset WSL so the Linux engine starts clean
        _run_hidden(["wsl", "--shutdown"])
        time.sleep(3)
        subprocess.Popen(
            [DOCKER_DESKTOP],
            startupinfo=hidden_startupinfo(), creationflags=NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

    if not Path(DOCKER_DESKTOP).exists():
        return {"ok": False, "error": "Docker Desktop not found at expected path"}
    threading.Thread(target=_restart, daemon=True).start()
    return {"ok": True, "message": "Restarting Docker Desktop — wait ~30 s then refresh"}


def restart_sunshine() -> dict[str, Any]:
    script = """
$ErrorActionPreference = 'Stop'
try {
    $service = Get-Service -Name 'SunshineService'
    Restart-Service -InputObject $service -ErrorAction Stop
    $service.WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
    Write-Output 'Sunshine restarted'
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
"""
    try:
        result = _run_hidden(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            text=True, timeout=45,
        )
        if result.returncode:
            return {"ok": False, "error": "Could not restart Sunshine. Run setup-sunshine-control.ps1 once as Administrator. " + (result.stderr.strip() or "Service control failed.")}
        return {"ok": True, "message": "Sunshine restarted. You can reconnect Moonlight."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Sunshine restart timed out. Check the Sunshine Service in Windows Services."}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def restart_machine() -> None:
    subprocess.Popen(
        ["shutdown", "/r", "/t", "10"],
        startupinfo=hidden_startupinfo(), creationflags=NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def tailscale_status() -> dict[str, Any]:
    """Return {connected, ip, hostname}. Safe on any failure / not installed."""
    offline = {"connected": False, "ip": None, "hostname": None}
    try:
        proc = _run_hidden(["tailscale", "status", "--json"], text=True, timeout=10)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return offline

    if proc.returncode != 0 or not proc.stdout.strip():
        return offline

    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return offline

    connected = (data.get("BackendState") or "") == "Running"
    self_node = data.get("Self") or {}
    ips = self_node.get("TailscaleIPs") or []
    ip = next((a for a in ips if ":" not in a), None) or (ips[0] if ips else None)
    hostname = self_node.get("HostName")
    return {"connected": connected, "ip": ip, "hostname": hostname}


def toggle_tailscale() -> dict[str, Any]:
    current = tailscale_status()
    cmd = ["tailscale", "down"] if current["connected"] else ["tailscale", "up"]
    try:
        _run_hidden(cmd, text=True, timeout=30)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass
    return tailscale_status()
