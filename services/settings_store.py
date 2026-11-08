"""settings.json persistence: Docker filter, per-app overrides, managed VMs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "settings.json"


def load() -> dict[str, Any]:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"docker": {"filter": []}}


def save(data: dict[str, Any]) -> None:
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def norm_path(p: str | None) -> str:
    return str(p).replace("\\", "/").rstrip("/").lower() if p else ""


def app_settings(name: str | None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = (settings if settings is not None else load()).get("apps", {})
    saved = cfg.get(name) if isinstance(cfg, dict) else None
    return saved if isinstance(saved, dict) else {}


def set_app_mode(name: str, mode: str) -> None:
    settings = load()
    apps_cfg = settings.get("apps")
    if not isinstance(apps_cfg, dict):
        apps_cfg = settings["apps"] = {}
    entry = apps_cfg.get(name)
    if not isinstance(entry, dict):
        entry = apps_cfg[name] = {}
    entry["mode"] = mode
    save(settings)


def docker_filter(settings: dict[str, Any] | None = None) -> list[str]:
    return (settings if settings is not None else load()).get("docker", {}).get("filter", [])


def set_docker_filter(filters: list[Any]) -> list[str]:
    settings = load()
    settings.setdefault("docker", {})["filter"] = [str(f) for f in filters if str(f).strip()]
    save(settings)
    return settings["docker"]["filter"]


def managed_vms() -> list[dict[str, Any]]:
    configured = load().get("vmware", {}).get("managed", [])
    if not isinstance(configured, list):
        return []
    return [vm for vm in configured if isinstance(vm, dict) and vm.get("vmx")]


def set_vm_desired_running(vmx: str, desired_running: bool) -> None:
    settings = load()
    managed = settings.get("vmware", {}).get("managed", [])
    if not isinstance(managed, list):
        return
    target = norm_path(vmx)
    for vm in managed:
        if isinstance(vm, dict) and norm_path(vm.get("vmx")) == target:
            vm["desiredRunning"] = desired_running
            save(settings)
            return
