"""Wrapper around VMware's `vmrun` CLI for listing and controlling VMs."""
from __future__ import annotations

import glob
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_NO_WINDOW = 0
try:
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
except AttributeError:
    _NO_WINDOW = 0

VMRUN_TIMEOUT = 30
HEALTH_TIMEOUT = 8
HEALTH_CACHE_SECONDS = 30

_health_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_VMRUN_GLOBS = [
    r"C:/Program Files*/VMware/*/vmrun.exe",
]


def _find_vmrun() -> str | None:
    for pattern in _VMRUN_GLOBS:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


def _run(vmrun: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [vmrun, *args],
        capture_output=True,
        text=True,
        timeout=VMRUN_TIMEOUT,
        creationflags=_NO_WINDOW,
    )


def _vm_name(vmx: str) -> str:
    return Path(vmx).stem


def _public_health(vm: dict[str, Any], running: bool) -> dict[str, Any] | None:
    health_url = vm.get("healthUrl")
    if not isinstance(health_url, str) or not health_url:
        return None
    if not running:
        return {"state": "waiting", "url": health_url, "detail": "VM stopped"}

    now = time.monotonic()
    cached = _health_cache.get(health_url)
    if cached and now - cached[0] < HEALTH_CACHE_SECONDS:
        return cached[1]

    expected_status = int(vm.get("healthExpectedStatus", 200))
    status: int | None = None
    detail = ""
    try:
        request = Request(health_url, headers={"User-Agent": "App-Dashboard-Healthcheck/1.0"})
        with urlopen(request, timeout=HEALTH_TIMEOUT) as response:
            status = response.status
    except HTTPError as exc:
        status = exc.code
    except (URLError, OSError, ValueError) as exc:
        detail = str(exc.reason) if isinstance(exc, URLError) else str(exc)

    result = {
        "state": "online" if status == expected_status else "offline",
        "url": health_url,
        "status": status,
        "detail": detail or (f"HTTP {status}" if status is not None else "Request failed"),
    }
    _health_cache[health_url] = (now, result)
    return result


def list_vms(managed_vms: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return running VMs plus configured VMs that are currently stopped."""
    vmrun = _find_vmrun()
    if not vmrun:
        return {"available": False, "vms": []}

    try:
        proc = _run(vmrun, ["list"])
    except subprocess.TimeoutExpired:
        return {"available": True, "error": "vmrun timed out", "vms": []}

    if proc.returncode != 0:
        return {
            "available": True,
            "error": (proc.stderr or "vmrun error").strip(),
            "vms": [],
        }

    managed_by_path = {
        str(Path(str(vm["vmx"]))).lower(): vm for vm in managed_vms or [] if vm.get("vmx")
    }
    vms: list[dict[str, Any]] = []
    running_paths: set[str] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("total running vms"):
            continue
        # vmrun list returns absolute .vmx paths for running VMs.
        if line.lower().endswith(".vmx"):
            normalized = str(Path(line)).lower()
            running_paths.add(normalized)
            configured = managed_by_path.get(normalized, {})
            entry = {
                "name": configured.get("name") or _vm_name(line),
                "vmx": line,
                "status": "running",
                "desiredRunning": bool(configured.get("desiredRunning")),
            }
            health = _public_health(configured, running=True)
            if health:
                entry["health"] = health
            vms.append(entry)
    for vm in managed_vms or []:
        vmx = str(vm.get("vmx") or "")
        if not vmx or str(Path(vmx)).lower() in running_paths:
            continue
        entry = {
            "name": vm.get("name") or _vm_name(vmx),
            "vmx": vmx,
            "status": "stopped",
            "desiredRunning": bool(vm.get("desiredRunning")),
        }
        health = _public_health(vm, running=False)
        if health:
            entry["health"] = health
        vms.append(entry)
    return {"available": True, "vms": vms}


def ensure_desired_running(managed_vms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Start configured, desired VMs that are not currently running."""
    current = list_vms()
    if not current.get("available") or current.get("error"):
        return []
    running_paths = {str(Path(vm["vmx"])).lower() for vm in current.get("vms", [])}
    results = []
    for vm in managed_vms:
        vmx = str(vm.get("vmx") or "")
        if not vm.get("desiredRunning") or not vmx or str(Path(vmx)).lower() in running_paths:
            continue
        results.append(start_vm(vmx, gui=False))
    return results


def start_vm(vmx: str, gui: bool = False) -> dict[str, Any]:
    mode = "gui" if gui else "nogui"
    return _control(["start", vmx, mode], f"start {_vm_name(vmx)}")


def stop_vm(vmx: str, force: bool = False) -> dict[str, Any]:
    mode = "hard" if force else "soft"
    return _control(["stop", vmx, mode], f"stop {_vm_name(vmx)}")


def _control(args: list[str], label: str) -> dict[str, Any]:
    vmrun = _find_vmrun()
    if not vmrun:
        return {"available": False, "ok": False, "error": "vmrun not found"}
    try:
        proc = _run(vmrun, args)
    except subprocess.TimeoutExpired:
        return {"available": True, "ok": False, "error": "vmrun timed out"}

    if proc.returncode == 0:
        return {"available": True, "ok": True, "message": label}
    return {
        "available": True,
        "ok": False,
        "error": (proc.stderr or proc.stdout or "vmrun error").strip(),
    }
