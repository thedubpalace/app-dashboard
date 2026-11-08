"""Thin wrapper around the docker CLI for listing and controlling containers."""
from __future__ import annotations

import json
import subprocess
from services.process_window import hidden_startupinfo
from typing import Any

_NO_WINDOW = 0
try:  # Hide the console window on Windows when invoking docker.
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
except AttributeError:
    _NO_WINDOW = 0

DOCKER_TIMEOUT = 15
DOCKER_RESTART_TIMEOUT = 30


def _run(args: list[str], timeout: int = DOCKER_TIMEOUT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        startupinfo=hidden_startupinfo(), creationflags=_NO_WINDOW,
    )


def _image_dates() -> dict[str, str]:
    """Return {image_ref: created_at_iso} for all locally available images."""
    try:
        proc = _run(["images", "--no-trunc", "--format", "{{json .}}"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    result: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        repo = obj.get("Repository") or ""
        tag  = obj.get("Tag") or ""
        created = obj.get("CreatedAt") or ""
        if not repo or repo == "<none>" or not created:
            continue
        full = f"{repo}:{tag}" if tag and tag != "<none>" else repo
        result[full] = created
        # also index bare name so "nginx" matches "nginx:latest"
        result[repo] = created
    return result


def list_containers() -> dict[str, Any]:
    """List all containers (running + stopped). `docker ps -a` as JSON lines."""
    try:
        proc = _run(["ps", "-a", "--format", "{{json .}}"])
    except FileNotFoundError:
        return {"available": False, "containers": []}
    except subprocess.TimeoutExpired:
        return {"available": True, "error": "docker timed out", "containers": []}

    if proc.returncode != 0:
        return {
            "available": True,
            "error": (proc.stderr or "docker error").strip(),
            "containers": [],
        }

    containers: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        state = (obj.get("State") or "").lower()
        status_text = obj.get("Status") or ""
        running = state == "running" or status_text.lower().startswith("up")
        containers.append(
            {
                "id": obj.get("ID"),
                "name": obj.get("Names"),
                "image": obj.get("Image"),
                "ports": obj.get("Ports") or "",
                "statusText": status_text,
                "status": "running" if running else "stopped",
            }
        )

    if containers:
        dates = _image_dates()
        for c in containers:
            img = c.get("image") or ""
            # try exact match, then with :latest appended
            created = dates.get(img) or dates.get(f"{img}:latest") or ""
            c["imageCreated"] = created

    return {"available": True, "containers": containers}


def container_states() -> dict[str, str]:
    """Cheap {name: "running"|"stopped"} map.

    `list_containers()` also shells out for image metadata, which is wasted
    work when a caller only needs to know whether a container is up.
    """
    try:
        proc = _run(["ps", "-a", "--format", "{{.Names}}\t{{.State}}"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    states: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        name, _, state = line.partition("\t")
        name = name.strip()
        if name:
            states[name] = "running" if state.strip().lower() == "running" else "stopped"
    return states


def start(name: str) -> dict[str, Any]:
    return _control("start", name)


def stop(name: str) -> dict[str, Any]:
    return _control("stop", name)


def restart(name: str) -> dict[str, Any]:
    return _control("restart", name, timeout=DOCKER_RESTART_TIMEOUT)


def _control(action: str, name: str, timeout: int = DOCKER_TIMEOUT) -> dict[str, Any]:
    try:
        proc = _run([action, name], timeout=timeout)
    except FileNotFoundError:
        return {"available": False, "ok": False, "error": "docker not installed"}
    except subprocess.TimeoutExpired:
        return {"available": True, "ok": False, "error": "docker timed out"}

    if proc.returncode == 0:
        return {"available": True, "ok": True, "message": f"{action} {name}"}
    return {
        "available": True,
        "ok": False,
        "error": (proc.stderr or proc.stdout or "docker error").strip(),
    }
