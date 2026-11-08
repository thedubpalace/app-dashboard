"""App run mode — "dev" (runCmd on this host) vs "docker" (the app's container).

An app that declares both a container and a real dev command can be flipped
between the two; the choice is persisted so start/stop/restart follow it.
"""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from services import docker_service, local_process, settings_store


def is_docker_cmd(run_cmd: str | None) -> bool:
    return bool(run_cmd) and run_cmd.strip().lower().startswith("docker")


def app_settings(entry: dict[str, Any], settings: dict[str, Any] | None = None) -> dict[str, Any]:
    return settings_store.app_settings(entry.get("name"), settings)


def container_for(entry: dict[str, Any], settings: dict[str, Any] | None = None) -> str | None:
    """The container backing this app.

    The app registry only names a container for apps that are deployed as
    one. Plenty of apps ship a Dockerfile *and* a dev server but have no
    registry link, so settings.json can map them here without editing the
    shared registry.
    """
    saved = app_settings(entry, settings)
    if saved.get("containerName"):
        return str(saved["containerName"])
    return entry.get("containerName") or None


def dev_cmd(entry: dict[str, Any], settings: dict[str, Any] | None = None) -> str | None:
    """The command that runs this app as a local dev server.

    Apps deployed as containers often have the *docker* command sitting in the
    registry's runCmd, which says nothing about how to run them locally — those
    keep their dev command in settings.json instead.
    """
    override = app_settings(entry, settings).get("devCmd")
    if override:
        return str(override)
    run_cmd = entry.get("runCmd")
    return None if is_docker_cmd(run_cmd) else (run_cmd or None)


def dev_port(entry: dict[str, Any], settings: dict[str, Any] | None = None) -> int | None:
    """Port the dev server binds. Needed when it differs from the registry's
    healthCheck target, which may describe the deployed container instead."""
    override = app_settings(entry, settings).get("devPort")
    if override:
        try:
            return int(override)
        except (TypeError, ValueError):
            pass
    return port_from_healthcheck(entry.get("healthCheck") or {}) or port_from_cmd(
        dev_cmd(entry, settings)
    )


def supports_modes(entry: dict[str, Any], settings: dict[str, Any] | None = None) -> bool:
    """True only when there is a genuine choice: a container *and* a way to run
    the app locally."""
    return bool(container_for(entry, settings)) and bool(dev_cmd(entry, settings))


def app_mode(
    entry: dict[str, Any],
    settings: dict[str, Any] | None = None,
    container_states: dict[str, str] | None = None,
) -> str:
    container = container_for(entry, settings)
    if not supports_modes(entry, settings):
        # Container-only entries can never run as a dev server.
        return "docker" if container else "dev"
    mode = app_settings(entry, settings).get("mode")
    if mode in ("dev", "docker"):
        return mode
    # Never switched from the dashboard — believe whatever is actually up, so
    # stop/restart act on the running side instead of a guessed default.
    if container_states is None:
        container_states = docker_service.container_states()
    return "docker" if container_states.get(container) == "running" else "dev"


def attach_run_modes(apps: list[dict[str, Any]]) -> None:
    """Tag each app with its run mode, and for container-backed apps whether
    that container is up — one `docker ps` shared across the whole list."""
    settings = settings_store.load()
    containers = {a.get("name"): container_for(a, settings) for a in apps}
    states = docker_service.container_states() if any(containers.values()) else {}
    for entry in apps:
        container = containers.get(entry.get("name"))
        port = dev_port(entry, settings)
        entry["containerName"] = container
        entry["devCmd"] = dev_cmd(entry, settings)
        entry["supportsModes"] = supports_modes(entry, settings)
        entry["mode"] = app_mode(entry, settings, states)
        if not container:
            continue
        entry["containerStatus"] = states.get(container)
        if entry["mode"] == "docker":
            # In docker mode the container is the source of truth. A registry
            # healthCheck port can point at the dev server's port, or at a port
            # some *other* container publishes, and then report a false "up".
            entry["status"] = "running" if entry["containerStatus"] == "running" else "stopped"
            entry["monitorable"] = True
        elif port and port != port_from_healthcheck(entry.get("healthCheck") or {}):
            # Dev server on a different port than the deployed container — the
            # registry healthCheck would be probing the container's port.
            entry["status"] = "running" if local_process.is_port_open(port) else "stopped"
            entry["monitorable"] = True


def port_from_healthcheck(hc: dict[str, Any]) -> int | None:
    if (hc.get("type") or "").lower() == "port":
        target = hc.get("target")
        if isinstance(target, int):
            return target
        if isinstance(target, str) and target.isdigit():
            return int(target)
    target = hc.get("target")
    if isinstance(target, str) and "://" in target:
        return urlparse(target).port
    return None


def port_from_cmd(run_cmd: str | None) -> int | None:
    if not run_cmd:
        return None
    tokens = run_cmd.replace("=", " ").split()
    for i, tok in enumerate(tokens):
        if tok in ("--port", "-p") and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            return int(tokens[i + 1])
    return None


def first_token(run_cmd: str | None) -> str | None:
    if not run_cmd:
        return None
    try:
        tokens = shlex.split(run_cmd, posix=False)
    except ValueError:
        tokens = run_cmd.split()
    if not tokens:
        return None
    name = Path(tokens[0].strip('"').strip("'")).name
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name
