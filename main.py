"""App Dashboard — unified control panel for registered apps, Docker, and VMware.

Run: uvicorn main:app --reload --port 8765
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from services import docker_service, nginx_proxy, app_registry, vmware_service

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "templates" / "index.html"
SETTINGS_FILE = BASE_DIR / "settings.json"


def _load_settings() -> dict[str, Any]:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"docker": {"filter": []}}


def _save_settings(data: dict[str, Any]) -> None:
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------
# App run mode — "dev" (runCmd on this host) vs "docker" (the app's container).
# An app that declares both a container and a real dev command can be flipped
# between the two; the choice is persisted so start/stop/restart follow it.
# ----------------------------------------------------------------------------
def _is_docker_cmd(run_cmd: str | None) -> bool:
    return bool(run_cmd) and run_cmd.strip().lower().startswith("docker")


def _container_for(entry: dict[str, Any], settings: dict[str, Any] | None = None) -> str | None:
    """The container backing this app.

    The app registry only names a container for apps that are deployed as
    one. Plenty of apps ship a Dockerfile *and* a dev server but have no
    registry link, so settings.json can map them here without editing the
    shared registry.
    """
    cfg = (settings if settings is not None else _load_settings()).get("apps", {})
    saved = cfg.get(entry.get("name")) if isinstance(cfg, dict) else None
    if isinstance(saved, dict) and saved.get("containerName"):
        return str(saved["containerName"])
    return entry.get("containerName") or None


def _app_settings(entry: dict[str, Any], settings: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = (settings if settings is not None else _load_settings()).get("apps", {})
    saved = cfg.get(entry.get("name")) if isinstance(cfg, dict) else None
    return saved if isinstance(saved, dict) else {}


def _dev_cmd(entry: dict[str, Any], settings: dict[str, Any] | None = None) -> str | None:
    """The command that runs this app as a local dev server.

    Apps deployed as containers often have the *docker* command sitting in the
    registry's runCmd, which says nothing about how to run them locally — those
    keep their dev command in settings.json instead.
    """
    override = _app_settings(entry, settings).get("devCmd")
    if override:
        return str(override)
    run_cmd = entry.get("runCmd")
    return None if _is_docker_cmd(run_cmd) else (run_cmd or None)


def _dev_port(entry: dict[str, Any], settings: dict[str, Any] | None = None) -> int | None:
    """Port the dev server binds. Needed when it differs from the registry's
    healthCheck target, which may describe the deployed container instead."""
    override = _app_settings(entry, settings).get("devPort")
    if override:
        try:
            return int(override)
        except (TypeError, ValueError):
            pass
    return _port_from_healthcheck(entry.get("healthCheck") or {}) or _port_from_cmd(
        _dev_cmd(entry, settings)
    )


def _supports_modes(entry: dict[str, Any], settings: dict[str, Any] | None = None) -> bool:
    """True only when there is a genuine choice: a container *and* a way to run
    the app locally."""
    return bool(_container_for(entry, settings)) and bool(_dev_cmd(entry, settings))


def _app_mode(
    entry: dict[str, Any],
    settings: dict[str, Any] | None = None,
    container_states: dict[str, str] | None = None,
) -> str:
    container = _container_for(entry, settings)
    if not _supports_modes(entry, settings):
        # Container-only entries can never run as a dev server.
        return "docker" if container else "dev"
    cfg = (settings if settings is not None else _load_settings()).get("apps", {})
    saved = cfg.get(entry.get("name")) if isinstance(cfg, dict) else None
    mode = saved.get("mode") if isinstance(saved, dict) else None
    if mode in ("dev", "docker"):
        return mode
    # Never switched from the dashboard — believe whatever is actually up, so
    # stop/restart act on the running side instead of a guessed default.
    if container_states is None:
        container_states = docker_service.container_states()
    return "docker" if container_states.get(container) == "running" else "dev"


def _set_app_mode(name: str, mode: str) -> None:
    settings = _load_settings()
    apps_cfg = settings.get("apps")
    if not isinstance(apps_cfg, dict):
        apps_cfg = settings["apps"] = {}
    entry = apps_cfg.get(name)
    if not isinstance(entry, dict):
        entry = apps_cfg[name] = {}
    entry["mode"] = mode
    _save_settings(settings)


def _managed_vms() -> list[dict[str, Any]]:
    configured = _load_settings().get("vmware", {}).get("managed", [])
    if not isinstance(configured, list):
        return []
    return [vm for vm in configured if isinstance(vm, dict) and vm.get("vmx")]


def _set_vm_desired_running(vmx: str, desired_running: bool) -> None:
    settings = _load_settings()
    managed = settings.get("vmware", {}).get("managed", [])
    if not isinstance(managed, list):
        return
    target = _norm_path(vmx)
    for vm in managed:
        if isinstance(vm, dict) and _norm_path(vm.get("vmx")) == target:
            vm["desiredRunning"] = desired_running
            _save_settings(settings)
            return


def _apply_docker_filter(containers: list[dict], filters: list[str]) -> list[dict]:
    if not filters:
        return containers
    f = [s.lower() for s in filters if s.strip()]
    if not f:
        return containers
    return [c for c in containers if any(kw in (c.get("name") or "").lower() for kw in f)]

app = FastAPI(title="App Dashboard")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

_vmware_watchdog_task: asyncio.Task | None = None


async def _vmware_watchdog() -> None:
    """Keep desired VMs running; Task Scheduler independently covers reboot."""
    await asyncio.sleep(30)
    while True:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, vmware_service.ensure_desired_running, _managed_vms())
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(60)


@app.on_event("startup")
async def start_vmware_watchdog() -> None:
    global _vmware_watchdog_task
    _vmware_watchdog_task = asyncio.create_task(_vmware_watchdog())


@app.on_event("shutdown")
async def stop_vmware_watchdog() -> None:
    if _vmware_watchdog_task:
        _vmware_watchdog_task.cancel()

_NO_WINDOW = 0
try:
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
except AttributeError:
    _NO_WINDOW = 0

_NEW_GROUP = 0
try:
    _NEW_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
except AttributeError:
    _NEW_GROUP = 0


# ----------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    # The page is fully client-rendered (data via /api/* fetches), so the
    # template has no server-side variables — serve it as static HTML. This
    # also sidesteps a Jinja2 template-cache bug on this Python/Jinja combo.
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return _load_settings()


@app.patch("/api/settings/docker/filter")
async def set_docker_filter(payload: dict[str, Any]) -> JSONResponse:
    filters = payload.get("filter")
    if not isinstance(filters, list):
        return JSONResponse({"ok": False, "error": "filter must be a list"}, status_code=400)
    settings = _load_settings()
    settings.setdefault("docker", {})["filter"] = [str(f) for f in filters if str(f).strip()]
    _save_settings(settings)
    return JSONResponse({"ok": True, "filter": settings["docker"]["filter"]})


# ----------------------------------------------------------------------------
# Aggregate status
# ----------------------------------------------------------------------------
@app.get("/api/status")
async def status() -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    processes = await loop.run_in_executor(None, app_registry.snapshot_processes)
    apps, docker, vmware = await asyncio.gather(
        loop.run_in_executor(None, app_registry.list_apps, processes),
        loop.run_in_executor(None, docker_service.list_containers),
        loop.run_in_executor(None, vmware_service.list_vms, _managed_vms()),
    )
    _attach_session_counts(apps, processes)

    filters = _load_settings().get("docker", {}).get("filter", [])
    if isinstance(docker, dict) and "containers" in docker:
        docker = dict(docker)
        docker["containers"] = _apply_docker_filter(docker["containers"], filters)
        docker["filtered"] = bool(filters)
        docker["filterList"] = filters
    return {"apps": apps, "docker": docker, "vmware": vmware}


# ----------------------------------------------------------------------------
# registered apps
# ----------------------------------------------------------------------------
def _apps_with_sessions() -> list[dict[str, Any]]:
    processes = app_registry.snapshot_processes()
    result = app_registry.list_apps(processes)
    _attach_session_counts(result, processes)
    _attach_run_modes(result)
    _attach_public_urls(result)
    return result


def _attach_public_urls(apps: list[dict[str, Any]]) -> None:
    """Link each app to the address it is reachable at from outside, as routed
    by Nginx Proxy Manager. Dev and container now share a port per app, so the
    same link works in either mode."""
    urls = nginx_proxy.public_urls()
    if not urls:
        return
    for entry in apps:
        port = _dev_port(entry)
        if port:
            entry["url"] = urls.get(port)


def _attach_run_modes(apps: list[dict[str, Any]]) -> None:
    """Tag each app with its run mode, and for container-backed apps whether
    that container is up — one `docker ps` shared across the whole list."""
    settings = _load_settings()
    containers = {a.get("name"): _container_for(a, settings) for a in apps}
    states = docker_service.container_states() if any(containers.values()) else {}
    for entry in apps:
        container = containers.get(entry.get("name"))
        dev_port = _dev_port(entry, settings)
        entry["containerName"] = container
        entry["devCmd"] = _dev_cmd(entry, settings)
        entry["supportsModes"] = _supports_modes(entry, settings)
        entry["mode"] = _app_mode(entry, settings, states)
        if not container:
            continue
        entry["containerStatus"] = states.get(container)
        if entry["mode"] == "docker":
            # In docker mode the container is the source of truth. A registry
            # healthCheck port can point at the dev server's port, or at a port
            # some *other* container publishes, and then report a false "up".
            entry["status"] = "running" if entry["containerStatus"] == "running" else "stopped"
            entry["monitorable"] = True
        elif dev_port and dev_port != _port_from_healthcheck(entry.get("healthCheck") or {}):
            # Dev server on a different port than the deployed container — the
            # registry healthCheck would be probing the container's port.
            entry["status"] = "running" if _is_port_open(dev_port) else "stopped"
            entry["monitorable"] = True


@app.get("/api/apps")
async def apps() -> list[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _apps_with_sessions)


@app.post("/api/apps/{name}/start")
async def app_start(name: str) -> JSONResponse:
    entry = app_registry.get_app(name)
    if not entry:
        return JSONResponse({"ok": False, "error": f"unknown app: {name}"}, status_code=404)

    container = _container_for(entry)
    if container and _app_mode(entry) == "docker":
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, docker_service.start, container)
        if result.get("ok"):
            result.setdefault("message", f"started container {container}")
        return JSONResponse(result)

    if not _dev_cmd(entry):
        return JSONResponse({"ok": False, "error": "no dev command configured"}, status_code=400)

    error = _start_local(entry)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=500)
    return JSONResponse({"ok": True, "message": f"started {name}"})


def _start_local(entry: dict[str, Any]) -> str | None:
    """Launch the app's dev command on this host. Returns an error, or None."""
    run_cmd = _dev_cmd(entry)
    if not run_cmd:
        return "no dev command configured"

    cwd = entry.get("localPath")
    if cwd and not Path(cwd).is_dir():
        cwd = None

    try:
        args = shlex.split(run_cmd, posix=False)
    except ValueError:
        args = run_cmd.split()

    try:
        subprocess.Popen(
            args,
            cwd=cwd,
            creationflags=_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        # Fall back to shell so PATH-resolved commands (e.g. uvicorn) work.
        try:
            subprocess.Popen(run_cmd, cwd=cwd, shell=True, creationflags=_NO_WINDOW)
        except Exception as exc:  # noqa: BLE001
            return str(exc)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


@app.post("/api/apps/{name}/restart")
async def app_restart(name: str) -> JSONResponse:
    entry = app_registry.get_app(name)
    if not entry:
        return JSONResponse({"ok": False, "error": f"unknown app: {name}"}, status_code=404)

    # Container-backed apps (dockerImage/containerName in the registry) are
    # restarted via `docker restart` — never by killing/re-running runCmd,
    # since runCmd for these entries is typically just the local-dev fallback.
    container_name = _container_for(entry)
    if container_name and _app_mode(entry) == "docker":
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, docker_service.restart, container_name)
        if result.get("ok"):
            result.setdefault("message", f"restarted container {container_name}")
        return JSONResponse(result)

    run_cmd = _dev_cmd(entry)
    if not run_cmd:
        return JSONResponse({"ok": False, "error": "no dev command configured"}, status_code=400)

    port: int | None = _dev_port(entry)
    token: str | None = None if port else _first_token(run_cmd)
    cwd: str | None = entry.get("localPath") or None

    # Spawn a detached Python process that does stop → wait → start.
    # Detached so it survives even when this server process is the one being restarted.
    script = _build_restart_script(port, token, run_cmd, cwd)
    try:
        subprocess.Popen(
            ["python", "-c", script],
            creationflags=_NO_WINDOW | _NEW_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True, "message": f"restarting {name}…"})


def _build_restart_script(port: int | None, token: str | None, run_cmd: str, cwd: str | None) -> str:
    return f"""
import time, subprocess, socket, shlex
import psutil

time.sleep(0.7)

port = {port!r}
token = {token!r}
run_cmd = {run_cmd!r}
cwd = {cwd!r}

if port:
    for conn in psutil.net_connections(kind='inet'):
        if conn.laddr and conn.laddr.port == port and conn.pid:
            try: psutil.Process(conn.pid).terminate()
            except: pass
else:
    needle = (token or '').lower()
    for proc in psutil.process_iter(['name']):
        try:
            pname = (proc.info.get('name') or '').lower()
            if pname.endswith('.exe'): pname = pname[:-4]
            if pname == needle: proc.terminate()
        except: pass

if port:
    for _ in range(30):
        time.sleep(0.25)
        s = socket.socket()
        s.settimeout(0.5)
        try:
            if s.connect_ex(('localhost', port)) != 0:
                break
        finally:
            s.close()
else:
    time.sleep(1.0)

try:
    args = shlex.split(run_cmd, posix=False)
except Exception:
    args = run_cmd.split()
try:
    subprocess.Popen(args, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
except FileNotFoundError:
    subprocess.Popen(run_cmd, cwd=cwd, shell=True)
"""


@app.post("/api/apps/{name}/shell")
async def app_shell(name: str) -> JSONResponse:
    entry = app_registry.get_app(name)
    if not entry:
        return JSONResponse({"ok": False, "error": f"unknown app: {name}"}, status_code=404)
    cwd = entry.get("localPath")
    if not cwd or not Path(cwd).is_dir():
        return JSONResponse({"ok": False, "error": "localPath not found"}, status_code=400)
    claude_exe = _find_claude_executable()
    if not claude_exe:
        return JSONResponse(
            {"ok": False, "error": "Claude Code CLI was not found. Install it or add it to PATH."},
            status_code=503,
        )
    try:
        safe_cwd = str(cwd).replace("'", "''")
        safe_claude = claude_exe.replace("'", "''")
        subprocess.Popen(
            [
                "powershell",
                "-NoExit",
                "-Command",
                f"Set-Location -LiteralPath '{safe_cwd}'; & '{safe_claude}' remote-control",
            ],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return JSONResponse({"ok": True, "message": f"opened shell for {name}"})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/apps/{name}/codex")
async def app_codex(name: str) -> JSONResponse:
    """Open an interactive Codex session in an app's local folder."""
    entry = app_registry.get_app(name)
    if not entry:
        return JSONResponse({"ok": False, "error": f"unknown app: {name}"}, status_code=404)
    cwd = entry.get("localPath")
    if not cwd or not Path(cwd).is_dir():
        return JSONResponse({"ok": False, "error": "localPath not found"}, status_code=400)
    codex_exe = _find_codex_executable()
    if not codex_exe:
        return JSONResponse(
            {"ok": False, "error": "Codex CLI was not found. Install or update the Codex desktop app."},
            status_code=503,
        )
    try:
        safe_cwd = str(cwd).replace("'", "''")
        safe_codex = codex_exe.replace("'", "''")
        subprocess.Popen(
            [
                "powershell",
                "-NoExit",
                "-Command",
                f"$env:HOME = $env:USERPROFILE; Set-Location -LiteralPath '{safe_cwd}'; & '{safe_codex}'",
            ],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return JSONResponse({"ok": True, "message": f"opened Codex for {name}"})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


def _find_claude_executable() -> str | None:
    """Find the Claude Code CLI even when this server's PATH predates its install."""
    from_path = shutil.which("claude") or shutil.which("claude.exe")
    if from_path:
        return from_path

    # The native installer puts claude.exe in ~/.local/bin; npm installs use
    # %APPDATA%\npm. Neither is guaranteed to be on a service's PATH.
    candidates = [
        Path.home() / ".local" / "bin" / "claude.exe",
        Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def _find_codex_executable() -> str | None:
    """Find the Codex CLI even when this server's PATH predates its install."""
    from_path = shutil.which("codex") or shutil.which("codex.exe")
    if from_path:
        return from_path

    # Codex Desktop installs versioned CLI binaries here on Windows. Searching
    # this narrow directory makes the launcher resilient to stale server PATHs.
    install_dir = Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin"
    try:
        candidates = [path for path in install_dir.glob("*/codex.exe") if path.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return str(max(candidates, key=lambda path: path.stat().st_mtime))


@app.post("/api/apps/{name}/stop")
async def app_stop(name: str) -> JSONResponse:
    entry = app_registry.get_app(name)
    if not entry:
        return JSONResponse({"ok": False, "error": f"unknown app: {name}"}, status_code=404)

    container = _container_for(entry)
    if container and _app_mode(entry) == "docker":
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, docker_service.stop, container)
        if result.get("ok"):
            result.setdefault("message", f"stopped container {container}")
        return JSONResponse(result)

    killed = _stop_local(entry)
    if killed:
        return JSONResponse({"ok": True, "message": f"stopped {name} ({killed} proc)"})
    return JSONResponse({"ok": False, "error": "no matching process found"}, status_code=404)


def _stop_local(entry: dict[str, Any]) -> int:
    """Kill the host processes serving this app. Returns how many were killed."""
    port = _dev_port(entry)

    killed = _kill_by_port(port) if port is not None else 0
    if killed == 0:
        # Fall back: match the first command token against process names.
        token = _first_token(_dev_cmd(entry))
        if token:
            killed += _kill_by_name(token)
    return killed


@app.post("/api/apps/{name}/mode")
async def app_set_mode(name: str, payload: dict[str, Any]) -> JSONResponse:
    """Flip an app between its dev server and its container, stopping whichever
    side is losing so the two never fight over the same port."""
    entry = app_registry.get_app(name)
    if not entry:
        return JSONResponse({"ok": False, "error": f"unknown app: {name}"}, status_code=404)

    mode = str(payload.get("mode") or "").lower()
    if mode not in ("dev", "docker"):
        return JSONResponse({"ok": False, "error": "mode must be 'dev' or 'docker'"}, status_code=400)
    if not _supports_modes(entry):
        return JSONResponse(
            {"ok": False, "error": f"{name} has no dev/docker choice to make"}, status_code=400
        )

    container = _container_for(entry)
    loop = asyncio.get_running_loop()
    _set_app_mode(name, mode)

    if mode == "docker":
        await loop.run_in_executor(None, _stop_local, entry)
        result = await loop.run_in_executor(None, docker_service.start, container)
        if not result.get("ok"):
            return JSONResponse(
                {"ok": False, "error": f"docker start {container}: {result.get('error')}"},
                status_code=500,
            )
        return JSONResponse({"ok": True, "message": f"{name} now running in Docker"})

    # dev: free the port the container holds before the dev server binds it.
    await loop.run_in_executor(None, docker_service.stop, container)
    error = await loop.run_in_executor(None, _start_local, entry)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=500)
    return JSONResponse({"ok": True, "message": f"{name} now running as dev server"})


# ----------------------------------------------------------------------------
# Docker
# ----------------------------------------------------------------------------
_PUBLISHED_PORT = re.compile(r":(\d+)->")


def _attach_container_urls(containers: list[dict[str, Any]]) -> None:
    urls = nginx_proxy.public_urls()
    if not urls:
        return
    for container in containers:
        candidates: list[str] = []
        for match in _PUBLISHED_PORT.finditer(container.get("ports") or ""):
            url = urls.get(int(match.group(1)))
            if url and url not in candidates:
                candidates.append(url)
        if not candidates:
            continue
        # A container can publish several ports — pourover-guide-app answers on
        # both the domain root and /coffee. The path-scoped route names this app
        # specifically, so it beats a root catch-all.
        container["url"] = next(
            (u for u in candidates if urlparse(u).path.strip("/")), candidates[0]
        )


def _docker_with_filter() -> dict[str, Any]:
    result = docker_service.list_containers()
    filters = _load_settings().get("docker", {}).get("filter", [])
    if isinstance(result, dict) and "containers" in result:
        result = dict(result)
        result["containers"] = _apply_docker_filter(result["containers"], filters)
        result["filtered"] = bool(filters)
        result["filterList"] = filters
        _attach_container_urls(result["containers"])
    return result


@app.get("/api/docker")
async def docker_list() -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _docker_with_filter)


@app.post("/api/docker/{name}/start")
async def docker_start(name: str) -> JSONResponse:
    return JSONResponse(docker_service.start(name))


@app.post("/api/docker/{name}/stop")
async def docker_stop(name: str) -> JSONResponse:
    return JSONResponse(docker_service.stop(name))


@app.post("/api/docker/{name}/restart")
async def docker_restart(name: str) -> JSONResponse:
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, docker_service.restart, name)
    return JSONResponse(result)


@app.post("/api/docker/fix")
async def docker_fix() -> JSONResponse:
    _DOCKER_DESKTOP = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
    import threading, time

    def _restart():
        # Kill all Docker Desktop processes
        subprocess.run(["taskkill", "/F", "/IM", "Docker Desktop.exe"], capture_output=True, creationflags=_NO_WINDOW)
        subprocess.run(["taskkill", "/F", "/IM", "com.docker.backend.exe"], capture_output=True, creationflags=_NO_WINDOW)
        # Reset WSL so the Linux engine starts clean
        subprocess.run(["wsl", "--shutdown"], capture_output=True, creationflags=_NO_WINDOW)
        time.sleep(3)
        subprocess.Popen(
            [_DOCKER_DESKTOP],
            creationflags=_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

    try:
        if not Path(_DOCKER_DESKTOP).exists():
            return JSONResponse({"ok": False, "error": "Docker Desktop not found at expected path"}, status_code=500)
        threading.Thread(target=_restart, daemon=True).start()
        return JSONResponse({"ok": True, "message": "Restarting Docker Desktop — wait ~30 s then refresh"})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ----------------------------------------------------------------------------
# System
# ----------------------------------------------------------------------------
@app.post("/api/system/restart")
async def system_restart() -> JSONResponse:
    try:
        subprocess.Popen(
            ["shutdown", "/r", "/t", "10"],
            creationflags=_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return JSONResponse({"ok": True, "message": "Machine restarting in 10 seconds…"})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ----------------------------------------------------------------------------
# VMware
# ----------------------------------------------------------------------------
@app.get("/api/vmware")
async def vmware_list() -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, vmware_service.list_vms, _managed_vms())


@app.post("/api/vmware/start")
async def vmware_start(payload: dict[str, Any]) -> JSONResponse:
    vmx = payload.get("vmx")
    if not vmx:
        return JSONResponse({"ok": False, "error": "vmx required"}, status_code=400)
    gui = bool(payload.get("gui", False))
    result = vmware_service.start_vm(vmx, gui)
    if result.get("ok"):
        _set_vm_desired_running(vmx, True)
    return JSONResponse(result)


@app.post("/api/vmware/stop")
async def vmware_stop(payload: dict[str, Any]) -> JSONResponse:
    vmx = payload.get("vmx")
    if not vmx:
        return JSONResponse({"ok": False, "error": "vmx required"}, status_code=400)
    force = bool(payload.get("force", False))
    # A dashboard-initiated stop is intentional, so do not immediately undo it
    # with the watchdog. Starting it again re-enables auto-recovery.
    _set_vm_desired_running(vmx, False)
    return JSONResponse(vmware_service.stop_vm(vmx, force))


# ----------------------------------------------------------------------------
# Tailscale
# ----------------------------------------------------------------------------
def _tailscale_status() -> dict[str, Any]:
    """Return {connected, ip, hostname}. Safe on any failure / not installed."""
    try:
        proc = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return {"connected": False, "ip": None, "hostname": None}

    if proc.returncode != 0 or not proc.stdout.strip():
        return {"connected": False, "ip": None, "hostname": None}

    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return {"connected": False, "ip": None, "hostname": None}

    connected = (data.get("BackendState") or "") == "Running"
    self_node = data.get("Self") or {}
    ips = self_node.get("TailscaleIPs") or []
    ip = next((a for a in ips if ":" not in a), None) or (ips[0] if ips else None)
    hostname = self_node.get("HostName")
    return {"connected": connected, "ip": ip, "hostname": hostname}


@app.get("/api/tailscale/status")
async def tailscale_status() -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _tailscale_status)


@app.post("/api/tailscale/toggle")
async def tailscale_toggle() -> JSONResponse:
    loop = asyncio.get_running_loop()
    current = await loop.run_in_executor(None, _tailscale_status)
    cmd = ["tailscale", "down"] if current["connected"] else ["tailscale", "up"]

    def _run() -> dict[str, Any]:
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=_NO_WINDOW,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            pass
        return _tailscale_status()

    new_status = await loop.run_in_executor(None, _run)
    return JSONResponse({"connected": new_status["connected"], "ip": new_status["ip"]})


# ----------------------------------------------------------------------------
# Claude remote-control sessions
# ----------------------------------------------------------------------------

def _norm_path(p: str | None) -> str:
    return str(p).replace("\\", "/").rstrip("/").lower() if p else ""


def _find_claude_sessions(
    app_path: str | None = None, processes: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Return one entry per PowerShell terminal running claude remote-control.

    Pass app_path to filter to sessions whose Set-Location matches that path.
    Pass processes (from app_registry.snapshot_processes()) to reuse an existing
    process scan instead of taking a fresh one.
    """
    norm_filter = _norm_path(app_path) if app_path else None
    if processes is None:
        processes = app_registry.snapshot_processes()
    results = []
    for proc in processes:
        name = (proc["name"] or "").lower().replace(".exe", "")
        if name not in ("powershell", "pwsh"):
            continue
        cmdline = " ".join(str(c) for c in proc["cmdline"])
        if "remote-control" not in cmdline or "claude" not in cmdline.lower():
            continue
        m = re.search(r"Set-Location\s+(?:-LiteralPath\s+)?'([^']+)'", cmdline)
        path = m.group(1) if m else None
        if norm_filter and _norm_path(path) != norm_filter:
            continue
        results.append({"pid": proc["pid"], "path": path})
    return results


def _find_codex_sessions(
    app_path: str | None = None, processes: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Return dashboard-launched Codex terminals, optionally for one app path."""
    norm_filter = _norm_path(app_path) if app_path else None
    if processes is None:
        processes = app_registry.snapshot_processes()
    results = []
    for proc in processes:
        name = (proc["name"] or "").lower().replace(".exe", "")
        if name not in ("powershell", "pwsh"):
            continue
        cmdline = " ".join(str(c) for c in proc["cmdline"])
        if "codex.exe" not in cmdline.lower():
            continue
        match = re.search(r"Set-Location\s+-LiteralPath\s+'([^']+)'", cmdline)
        path = match.group(1) if match else None
        if not path or (norm_filter and _norm_path(path) != norm_filter):
            continue
        results.append({"pid": proc["pid"], "path": path})
    return results


def _attach_session_counts(apps: list[dict[str, Any]], processes: list[dict[str, Any]]) -> None:
    for key, finder in (("claudeSessions", _find_claude_sessions), ("codexSessions", _find_codex_sessions)):
        counts: dict[str, int] = {}
        for session in finder(processes=processes):
            path = _norm_path(session.get("path"))
            if path:
                counts[path] = counts.get(path, 0) + 1
        for app in apps:
            app[key] = counts.get(_norm_path(app.get("localPath")), 0)


@app.post("/api/apps/{name}/claude-sessions/kill-all")
async def kill_app_claude_sessions(name: str) -> JSONResponse:
    entry = app_registry.get_app(name)
    if not entry:
        return JSONResponse({"ok": False, "error": f"unknown app: {name}"}, status_code=404)
    sessions = _find_claude_sessions(app_path=entry.get("localPath"))
    killed = 0
    for s in sessions:
        try:
            psutil.Process(s["pid"]).terminate()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    msg = f"terminated {killed} session(s)" if killed else "no sessions found"
    return JSONResponse({"ok": True, "killed": killed, "message": msg})


@app.post("/api/apps/{name}/codex-sessions/kill-all")
async def kill_app_codex_sessions(name: str) -> JSONResponse:
    entry = app_registry.get_app(name)
    if not entry:
        return JSONResponse({"ok": False, "error": f"unknown app: {name}"}, status_code=404)
    sessions = _find_codex_sessions(app_path=entry.get("localPath"))
    killed = 0
    for session in sessions:
        try:
            root = psutil.Process(session["pid"])
            processes = root.children(recursive=True) + [root]
            for process in processes:
                process.terminate()
            gone, alive = psutil.wait_procs(processes, timeout=2)
            for process in alive:
                process.kill()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    msg = f"terminated {killed} Codex session(s)" if killed else "no Codex sessions found"
    return JSONResponse({"ok": True, "killed": killed, "message": msg})


# ----------------------------------------------------------------------------
# Helpers for stopping registered apps
# ----------------------------------------------------------------------------
def _port_from_healthcheck(hc: dict[str, Any]) -> int | None:
    if (hc.get("type") or "").lower() == "port":
        target = hc.get("target")
        if isinstance(target, int):
            return target
        if isinstance(target, str) and target.isdigit():
            return int(target)
    target = hc.get("target")
    if isinstance(target, str) and "://" in target:
        from urllib.parse import urlparse

        return urlparse(target).port
    return None


def _port_from_cmd(run_cmd: str | None) -> int | None:
    if not run_cmd:
        return None
    tokens = run_cmd.replace("=", " ").split()
    for i, tok in enumerate(tokens):
        if tok in ("--port", "-p") and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            return int(tokens[i + 1])
    return None


def _first_token(run_cmd: str | None) -> str | None:
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


# Ports published by a container are held by Docker's proxy processes, not by
# the app. Killing one of those takes Docker Desktop down with every container
# on it — always stop the container instead.
_DOCKER_PROXY_PROCESSES = frozenset(
    {
        "com.docker.backend",
        "com.docker.proxy",
        "docker",
        "dockerd",
        "docker desktop",
        "vpnkit",
        "wslrelay",
        "wslhost",
        "wsl",
    }
)


def _is_docker_proxy(proc: psutil.Process) -> bool:
    try:
        name = proc.name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if name.endswith(".exe"):
        name = name[:-4]
    return name in _DOCKER_PROXY_PROCESSES


def _kill_by_port(port: int) -> int:
    killed = 0
    for conn in psutil.net_connections(kind="inet"):
        if conn.laddr and conn.laddr.port == port and conn.pid:
            try:
                proc = psutil.Process(conn.pid)
                if _is_docker_proxy(proc):
                    continue
                proc.terminate()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    return killed


def _kill_by_name(name: str) -> int:
    needle = name.lower()
    killed = 0
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            pname = (proc.info.get("name") or "").lower()
            if pname.endswith(".exe"):
                pname = pname[:-4]
            if pname == needle:
                proc.terminate()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed


def _is_port_open(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        return sock.connect_ex(("localhost", port)) == 0
    finally:
        sock.close()
