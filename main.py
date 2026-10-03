"""Veda Dashboard: read-only inventory plus a Temporal lifecycle facade."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from services import docker_service, nginx_proxy, veda_apps, vmware_service
from services.dashboard_auth import COOKIE, configured, create_session, credentials_valid, csrf_valid, read_session
from services.temporal_client import TemporalClientError, client

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "templates" / "index.html"
SETTINGS_FILE = BASE_DIR / "settings.json"
app = FastAPI(title="Veda Dashboard")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
_login_failures: dict[str, list[float]] = {}


def _public_url(name: str, fallback: str) -> str:
    value = os.getenv(name, fallback).rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return fallback.rstrip("/")
    return value


TEMPORAL_UI_URL = _public_url("TEMPORAL_UI_PUBLIC_URL", "https://pulsenprompt.duckdns.org/temporal-ui/")
TEMPORAL_ADMIN_URL = _public_url("TEMPORAL_ADMIN_PUBLIC_URL", "https://pulsenprompt.duckdns.org/temporal-ops/")
TEMPORAL_NAMESPACE = os.getenv("TEMPORAL_NAMESPACE", "default")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Permissions-Policy": "camera=(), microphone=(), geolocation=(), usb=()", "Content-Security-Policy": "default-src 'self' https://fonts.googleapis.com https://fonts.gstatic.com; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
    return response


def _load_settings() -> dict[str, Any]:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"docker": {"filter": []}}


def _save_settings(data: dict[str, Any]) -> None:
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _managed_vms() -> list[dict[str, Any]]:
    values = _load_settings().get("vmware", {}).get("managed", [])
    return [vm for vm in values if isinstance(vm, dict) and vm.get("vmx")] if isinstance(values, list) else []


def _norm(value: str | None) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").lower()


def _apply_docker_filter(items: list[dict[str, Any]], filters: list[str]) -> list[dict[str, Any]]:
    terms = [item.lower() for item in filters if item.strip()]
    return items if not terms else [item for item in items if any(term in str(item.get("name", "")).lower() for term in terms)]


def _control_snapshot() -> dict[str, Any]:
    try:
        return client.resources(actor="dashboard-read")
    except TemporalClientError as exc:
        return {"resources": [], "registrySync": {"status": "unavailable", "error": str(exc)}}


def _surface_index(snapshot: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for resource in snapshot.get("resources", []):
        for ref in resource.get("surfaceRefs", []):
            if isinstance(ref, dict) and ref.get("surface") and ref.get("key"):
                index[(str(ref["surface"]), _norm(str(ref["key"])))] = resource
    return index


def _decorate(item: dict[str, Any], resource: dict[str, Any] | None) -> None:
    if not resource:
        item.update({"classification": "unmanaged", "capabilities": [], "unsupportedReason": "Not present in the signed Veda allowlist"})
        return
    item.update({"resourceId": resource.get("id"), "resourceRevision": resource.get("revision"), "classification": resource.get("classification", "managed"), "capabilities": resource.get("capabilities", []), "unsupportedReason": resource.get("unsupportedReason"), "controlStatus": resource.get("state"), "controlHealth": resource.get("health"), "observationError": resource.get("observationError")})


def _session_or_error(request: Request, *, check_csrf: bool = True) -> tuple[dict[str, str] | None, JSONResponse | None]:
    value = read_session(request)
    if not value:
        return None, JSONResponse({"ok": False, "error": "authentication required"}, status_code=401)
    if check_csrf and not csrf_valid(request, value):
        return None, JSONResponse({"ok": False, "error": "invalid CSRF token"}, status_code=403)
    return value, None


def _resource_for(surface: str, key: str) -> dict[str, Any] | None:
    return _surface_index(_control_snapshot()).get((surface, _norm(key)))


def _temporal_error(exc: TemporalClientError) -> JSONResponse:
    return JSONResponse(exc.payload, status_code=exc.status)


def _workflow_url(workflow: dict[str, Any]) -> str | None:
    workflow_id = str(workflow.get("workflowId") or "")
    if not workflow_id:
        return None
    root = f"{TEMPORAL_UI_URL}/namespaces/{quote(TEMPORAL_NAMESPACE, safe='')}/workflows/{quote(workflow_id, safe='')}"
    run_id = str(workflow.get("runId") or "")
    return f"{root}/{quote(run_id, safe='')}/history" if run_id else root


def _compact_message(workflow: dict[str, Any], state: str) -> str:
    status = str(workflow.get("status") or "")
    message = re.sub(r"\s+", " ", str(workflow.get("message") or "")).strip()
    if status.lower() == "retrying":
        return message[:180] or "Retrying"
    if status.lower() == "rolled back":
        return "Previous state restored"
    if status.lower() == "rollback failed":
        return "Rollback failed · inspect in Temporal"
    if status.lower() == "timed out":
        return "Timed out · inspect in Temporal"
    if state == "Pending":
        return "Accepted by Temporal"
    if state == "Running":
        return message[:180] or "Temporal is applying the change"
    if state == "Done":
        return "Health confirmed"
    return (message or status or "Request failed")[:180]


def _project_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    status = str(workflow.get("status") or "").lower()
    if status in {"accepted", "queued", "scheduled"}:
        state = "Pending"
    elif status in {"running", "retrying", "rollback in progress", "rollback-in-progress"}:
        state = "Running"
    elif status in {"succeeded", "rolled back"}:
        state = "Done"
    else:
        state = "Failed"
    return {
        "resourceId": str(workflow.get("resourceId") or ""),
        "operation": str(workflow.get("operation") or "action"),
        "state": state,
        "message": _compact_message(workflow, state),
        "temporalUrl": _workflow_url(workflow),
        "updatedAt": workflow.get("updatedAt"),
        "duplicate": bool(workflow.get("duplicate")),
    }


def _submit(request: Request, resource: dict[str, Any] | None, action: str, *, target_mode: str | None = None) -> JSONResponse:
    session, error = _session_or_error(request)
    if error:
        return error
    if not resource:
        return JSONResponse({"ok": False, "error": "resource is unmanaged or stale"}, status_code=422)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(resource["id"]))
    key = request.headers.get("idempotency-key") or f"dashboard_{safe_id}_{action}_{uuid.uuid4().hex}"
    if not re.fullmatch(r"[A-Za-z0-9_-]{12,128}", key):
        return JSONResponse({"ok": False, "error": "invalid Idempotency-Key"}, status_code=400)
    try:
        result = client.submit(str(resource["id"]), action, key, actor=session["username"], target_mode=target_mode)
        projection_source = {"resourceId": str(resource["id"]), "operation": action, **result}
        return JSONResponse({**result, "currentAction": _project_workflow(projection_source)}, status_code=202)
    except TemporalClientError as exc:
        return _temporal_error(exc)


def _unsupported() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "This auxiliary action is disabled until it has a dedicated allowlisted adapter"}, status_code=501)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@app.post("/api/login")
async def login(request: Request, payload: dict[str, Any]) -> JSONResponse:
    if not configured():
        return JSONResponse({"ok": False, "error": "dashboard authentication is not configured"}, status_code=503)
    username, password = str(payload.get("username", "")), str(payload.get("password", ""))
    window = int(os.getenv("DASHBOARD_LOGIN_RATE_WINDOW_SECONDS", "900"))
    maximum = int(os.getenv("DASHBOARD_LOGIN_RATE_ATTEMPTS", "5"))
    key = f"{request.client.host if request.client else 'unknown'}:{username.lower()}"
    now = time.time()
    attempts = [value for value in _login_failures.get(key, []) if now - value < window]
    if len(attempts) >= maximum:
        return JSONResponse({"ok": False, "error": "too many login attempts"}, status_code=429, headers={"Retry-After": str(window)})
    if not credentials_valid(username, password):
        _login_failures[key] = [*attempts, now]
        return JSONResponse({"ok": False, "error": "invalid credentials"}, status_code=401)
    _login_failures.pop(key, None)
    value, csrf_token = create_session(username)
    response = JSONResponse({"ok": True, "username": username, "csrfToken": csrf_token})
    response.set_cookie(COOKIE, value, httponly=True, secure=os.getenv("DASHBOARD_COOKIE_SECURE", "0") == "1", samesite="strict", max_age=12 * 60 * 60)
    return response


@app.get("/api/session")
async def session(request: Request) -> JSONResponse:
    value = read_session(request)
    return JSONResponse({"authenticated": bool(value), "username": value.get("username") if value else None, "csrfToken": value.get("csrf") if value else None})


@app.post("/api/logout")
async def logout(request: Request) -> JSONResponse:
    value, error = _session_or_error(request)
    if error:
        return error
    response = JSONResponse({"ok": True, "username": value["username"]})
    response.delete_cookie(COOKIE)
    return response


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return _load_settings()


@app.patch("/api/settings/docker/filter")
async def set_docker_filter(request: Request, payload: dict[str, Any]) -> JSONResponse:
    _, error = _session_or_error(request)
    if error:
        return error
    filters = payload.get("filter")
    if not isinstance(filters, list):
        return JSONResponse({"ok": False, "error": "filter must be a list"}, status_code=400)
    settings = _load_settings()
    settings.setdefault("docker", {})["filter"] = [str(item) for item in filters if str(item).strip()]
    _save_settings(settings)
    return JSONResponse({"ok": True, "filter": settings["docker"]["filter"]})


def _apps() -> list[dict[str, Any]]:
    items = veda_apps.list_apps(veda_apps.snapshot_processes())
    index, urls = _surface_index(_control_snapshot()), nginx_proxy.public_urls()
    for item in items:
        _decorate(item, index.get(("app", _norm(item.get("name")))))
        hc = item.get("healthCheck") or {}
        if str(hc.get("type", "")).lower() == "port":
            try:
                item["url"] = urls.get(int(hc.get("target")))
            except (TypeError, ValueError):
                pass
    return items


def _docker() -> dict[str, Any]:
    result = docker_service.list_containers()
    filters = _load_settings().get("docker", {}).get("filter", [])
    index, urls = _surface_index(_control_snapshot()), nginx_proxy.public_urls()
    containers = _apply_docker_filter(result.get("containers", []), filters)
    for item in containers:
        _decorate(item, index.get(("docker", _norm(item.get("name")))))
        matches = [urls.get(int(port)) for port in re.findall(r":(\d+)->", str(item.get("ports", ""))) if urls.get(int(port))]
        if matches:
            item["url"] = next((url for url in matches if urlparse(url).path.strip("/")), matches[0])
    return {**result, "containers": containers, "filtered": bool(filters), "filterList": filters}


def _vmware() -> dict[str, Any]:
    result = vmware_service.list_vms(_managed_vms())
    index = _surface_index(_control_snapshot())
    for item in result.get("vms", []):
        _decorate(item, index.get(("vmware", _norm(item.get("vmx")))))
    return result


@app.get("/api/apps")
async def apps() -> list[dict[str, Any]]:
    return await asyncio.get_running_loop().run_in_executor(None, _apps)


@app.get("/api/docker")
async def docker_list() -> dict[str, Any]:
    return await asyncio.get_running_loop().run_in_executor(None, _docker)


@app.get("/api/vmware")
async def vmware_list() -> dict[str, Any]:
    return await asyncio.get_running_loop().run_in_executor(None, _vmware)


@app.get("/api/status")
async def status() -> dict[str, Any]:
    veda, docker, vmware = await asyncio.gather(apps(), docker_list(), vmware_list())
    return {"veda": veda, "docker": docker, "vmware": vmware}


@app.get("/api/control/readiness")
async def readiness() -> dict[str, Any]:
    sync = _control_snapshot().get("registrySync", {})
    return {"ok": sync.get("status") == "ready", **sync}


@app.get("/api/actions")
async def current_actions(request: Request) -> JSONResponse:
    _, error = _session_or_error(request, check_csrf=False)
    if error:
        return error
    try:
        rows = client.workflows(actor="dashboard-read")
        workflows = rows if isinstance(rows, list) else rows.get("workflows", [])
        latest: dict[str, dict[str, Any]] = {}
        for workflow in sorted(workflows, key=lambda item: str(item.get("updatedAt") or ""), reverse=True):
            resource_id = str(workflow.get("resourceId") or "")
            if resource_id and resource_id not in latest:
                latest[resource_id] = _project_workflow(workflow)
        return JSONResponse({"actions": list(latest.values())})
    except TemporalClientError as exc:
        return _temporal_error(exc)


@app.get("/api/navigation")
async def navigation(request: Request) -> JSONResponse:
    session_value = read_session(request)
    if not session_value:
        return JSONResponse({"ok": False, "error": "authentication required"}, status_code=401)
    return JSONResponse({"temporalUiUrl": f"{TEMPORAL_UI_URL}/", "adminUrl": f"{TEMPORAL_ADMIN_URL}/", "showAdmin": True})


@app.get("/api/workflows")
async def workflows() -> JSONResponse:
    try:
        rows = client.workflows()
        return JSONResponse({"workflows": rows if isinstance(rows, list) else rows.get("workflows", [])})
    except TemporalClientError as exc:
        return _temporal_error(exc)


@app.get("/api/history")
async def history() -> JSONResponse:
    try:
        rows = client.audit()
        return JSONResponse({"history": rows if isinstance(rows, list) else rows.get("history", [])})
    except TemporalClientError as exc:
        return _temporal_error(exc)


@app.post("/api/registry/sync")
async def registry_sync(request: Request) -> JSONResponse:
    value, error = _session_or_error(request)
    if error:
        return error
    try:
        return JSONResponse(client.sync_registry(actor=value["username"]))
    except TemporalClientError as exc:
        return _temporal_error(exc)


@app.post("/api/apps/{name}/mode")
async def app_mode(name: str, request: Request, payload: dict[str, Any]) -> JSONResponse:
    mode = str(payload.get("mode", ""))
    if not re.fullmatch(r"[a-z0-9._-]{2,32}", mode):
        return JSONResponse({"ok": False, "error": "invalid mode"}, status_code=400)
    return _submit(request, _resource_for("mode", name) or _resource_for("app-mode", name), "switch-mode", target_mode=mode)


@app.post("/api/apps/{name}/{action}")
async def app_lifecycle(name: str, action: str, request: Request) -> JSONResponse:
    if action in {"shell", "codex"}:
        return _unsupported()
    if action not in {"start", "stop", "restart"}:
        return JSONResponse({"ok": False, "error": "unsupported action"}, status_code=404)
    return _submit(request, _resource_for("app", name), action)


@app.post("/api/docker/{name}/{action}")
async def docker_lifecycle(name: str, action: str, request: Request) -> JSONResponse:
    if action not in {"start", "stop", "restart"}:
        return JSONResponse({"ok": False, "error": "unsupported action"}, status_code=404)
    return _submit(request, _resource_for("docker", name), action)


@app.post("/api/vmware/{action}")
async def vmware_lifecycle(action: str, request: Request, payload: dict[str, Any]) -> JSONResponse:
    if action not in {"start", "stop", "restart"}:
        return JSONResponse({"ok": False, "error": "unsupported action"}, status_code=404)
    return _submit(request, _resource_for("vmware", str(payload.get("vmx", ""))), action)


@app.post("/api/docker/fix")
@app.post("/api/system/restart")
@app.post("/api/tailscale/toggle")
async def unsupported_auxiliary() -> JSONResponse:
    return _unsupported()


@app.post("/api/apps/{name}/claude-sessions/kill-all")
@app.post("/api/apps/{name}/codex-sessions/kill-all")
async def unsupported_session_kill(name: str) -> JSONResponse:
    del name
    return _unsupported()


def _tailscale_status() -> dict[str, Any]:
    try:
        completed = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10, shell=False)
        data = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        data = {}
    addresses = (data.get("Self") or {}).get("TailscaleIPs") or []
    return {"connected": data.get("BackendState") == "Running", "ip": next((value for value in addresses if ":" not in value), None), "hostname": (data.get("Self") or {}).get("HostName")}


@app.get("/api/tailscale/status")
async def tailscale_status() -> dict[str, Any]:
    return await asyncio.get_running_loop().run_in_executor(None, _tailscale_status)
