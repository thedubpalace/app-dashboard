"""HTTP surface for the Requirement Review Queue (`/review`, `/api/review/*`).

Every API endpoint requires a dashboard session (401), the
`requirement.review` permission = Admin role + reviewer allowlist (403), and
mutations require the existing CSRF header (403). The GitHub token, if any,
never leaves the server.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services import app_registry
from services.dashboard_auth import COOKIE, csrf_valid, read_session, role_for, session_fingerprint
from services.review_service import ReviewConfig, ReviewError, ReviewService, build_tracker, tracker_error_payload
from services.review_tracker import SIMULATIONS, MockTracker, TrackerError

BASE_DIR = Path(__file__).resolve().parent.parent
REVIEW_HTML = BASE_DIR / "templates" / "review.html"
PERMISSION = "requirement.review"
NO_STORE = {"Cache-Control": "no-store"}

router = APIRouter()
_services: dict[tuple[Any, ...], ReviewService] = {}


def get_service() -> ReviewService:
    config = ReviewConfig.from_env(Path(app_registry.REGISTRY_PATH))
    key = (config.mode, str(config.runtime_dir), str(config.audit_path), str(config.projects_dir), tuple(config.mock_projects),
           tuple(config.allowlist), config.required_approvals, os.getenv("REVIEW_GITHUB_TOKEN") is not None)
    service = _services.get(key)
    if service is None:
        service = ReviewService(config, build_tracker(config))
        _services.clear()
        _services[key] = service
    return service


def _security_log(request: Request, status: int, code: str, username: str | None) -> None:
    """Record rejected review attempts (REQ-015: 401/403 go to the security log)."""
    path = Path(os.getenv("DASHBOARD_SECURITY_LOG", str(BASE_DIR / ".runtime" / "security.log")))
    try:
        from datetime import datetime, timezone
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"), "surface": "review",
                 "permission": PERMISSION, "method": request.method, "path": request.url.path, "status": status, "code": code,
                 "username": username, "client": request.client.host if request.client else None}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _deny(request: Request, status: int, code: str, message: str, username: str | None = None) -> JSONResponse:
    _security_log(request, status, code, username)
    return JSONResponse({"ok": False, "code": code, "error": message}, status_code=status, headers=NO_STORE)


def _authorize(request: Request, *, mutation: bool) -> tuple[dict[str, str] | None, JSONResponse | None]:
    session = read_session(request)
    if not session:
        if request.cookies.get(COOKIE):
            return None, _deny(request, 401, "session_expired", "เซสชันหมดอายุ — เข้าสู่ระบบอีกครั้ง")
        return None, _deny(request, 401, "unauthenticated", "ต้องเข้าสู่ระบบก่อน")
    username = session["username"]
    role = role_for(username)
    if not get_service().is_reviewer(username, role):
        return None, _deny(request, 403, "forbidden", "บัญชีนี้ไม่มีสิทธิ์รีวิว requirement", username)
    if mutation and not csrf_valid(request, session):
        return None, _deny(request, 403, "csrf_invalid", "CSRF token ไม่ถูกต้อง — รีเฟรชหน้าแล้วลองใหม่", username)
    return {"username": username, "role": role, "sessionId": session_fingerprint(request)}, None


def _json(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=NO_STORE)


def _failure(exc: Exception) -> JSONResponse:
    if isinstance(exc, ReviewError):
        return _json(exc.payload(), exc.status)
    if isinstance(exc, TrackerError):
        return _json(tracker_error_payload(exc), exc.status)
    raise exc


def _reviewer(ctx: dict[str, str]) -> dict[str, str]:
    return {"username": ctx["username"], "role": ctx["role"]}


@router.get("/review", response_class=HTMLResponse)
def review_page() -> HTMLResponse:
    return HTMLResponse(REVIEW_HTML.read_text(encoding="utf-8"), headers=NO_STORE)


@router.get("/api/review/queue")
def review_queue(request: Request) -> JSONResponse:
    ctx, error = _authorize(request, mutation=False)
    if error:
        return error
    service = get_service()
    meta = {"mode": service.config.mode, "reviewer": _reviewer(ctx), "debugAvailable": service.config.mode == "mock"}
    try:
        return _json({**service.queue(), **meta})
    except (ReviewError, TrackerError) as exc:
        response = _failure(exc)
        body = json.loads(response.body)
        return _json({**body, **meta}, response.status_code)


@router.get("/api/review/summary")
def review_summary(request: Request) -> JSONResponse:
    ctx, error = _authorize(request, mutation=False)
    if error:
        return error
    try:
        pending = len(get_service().tracker.list_pending())
    except TrackerError as exc:
        return _json({"ok": False, "code": exc.code, "pending": None, "url": "/review"})
    return _json({"ok": True, "pending": pending, "url": "/review", "mode": get_service().config.mode})


@router.get("/api/review/cards/{req_id}")
def review_card(req_id: str, request: Request) -> JSONResponse:
    ctx, error = _authorize(request, mutation=False)
    if error:
        return error
    try:
        return _json(get_service().card(req_id))
    except (ReviewError, TrackerError) as exc:
        return _failure(exc)


def _decision_input(payload: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
    seen_hash = str(payload.get("hash") or "")
    sections = payload.get("sectionHashes")
    if not seen_hash or len(seen_hash) > 128:
        raise ReviewError("invalid_request", "ไม่มี content hash ของฉบับที่อ่าน", 400)
    return seen_hash, ({str(k): str(v) for k, v in sections.items()} if isinstance(sections, dict) else None)


@router.post("/api/review/cards/{req_id}/approve")
def review_approve(req_id: str, request: Request, payload: dict[str, Any]) -> JSONResponse:
    ctx, error = _authorize(request, mutation=True)
    if error:
        return error
    try:
        seen_hash, sections = _decision_input(payload)
        # Permission is re-checked server-side on every approve via _authorize above.
        return _json(get_service().approve(actor=ctx["username"], role=ctx["role"], session_id=ctx["sessionId"], req_id=req_id,
                                           seen_hash=seen_hash, seen_sections=sections, key=request.headers.get("idempotency-key", "")))
    except (ReviewError, TrackerError) as exc:
        return _failure(exc)


@router.post("/api/review/cards/{req_id}/request-changes")
def review_request_changes(req_id: str, request: Request, payload: dict[str, Any]) -> JSONResponse:
    ctx, error = _authorize(request, mutation=True)
    if error:
        return error
    try:
        seen_hash, sections = _decision_input(payload)
        return _json(get_service().request_changes(actor=ctx["username"], role=ctx["role"], session_id=ctx["sessionId"], req_id=req_id,
                                                   seen_hash=seen_hash, seen_sections=sections, reason=str(payload.get("reason") or ""),
                                                   key=request.headers.get("idempotency-key", "")))
    except (ReviewError, TrackerError) as exc:
        return _failure(exc)


@router.post("/api/review/reconcile")
def review_reconcile(request: Request, payload: dict[str, Any]) -> JSONResponse:
    ctx, error = _authorize(request, mutation=True)
    if error:
        return error
    project = str(payload.get("project") or "")
    service = get_service()
    try:
        if project not in service.tracker.projects():
            return _json({"ok": False, "code": "not_found", "error": "ไม่พบโปรเจกต์นี้ใน tracker"}, 404)
        return _json({"ok": True, "reconcile": service.reconcile(project, actor=ctx["username"])})
    except (ReviewError, TrackerError) as exc:
        return _failure(exc)


def _mock_tracker() -> MockTracker | None:
    tracker = get_service().tracker
    return tracker if isinstance(tracker, MockTracker) else None


@router.get("/api/review/debug")
def review_debug_state(request: Request) -> JSONResponse:
    ctx, error = _authorize(request, mutation=False)
    if error:
        return error
    tracker = _mock_tracker()
    if not tracker:
        return _json({"ok": False, "code": "not_available", "error": "debug ใช้ได้เฉพาะโหมดจำลอง"}, 404)
    return _json({"ok": True, "simulate": tracker.simulation(), "options": SIMULATIONS})


@router.post("/api/review/debug")
def review_debug(request: Request, payload: dict[str, Any]) -> JSONResponse:
    """Admin-only fault injection for the mock tracker (never available in github mode)."""
    ctx, error = _authorize(request, mutation=True)
    if error:
        return error
    tracker = _mock_tracker()
    if not tracker:
        return _json({"ok": False, "code": "not_available", "error": "debug ใช้ได้เฉพาะโหมดจำลอง"}, 404)
    action = str(payload.get("action") or "simulate")
    if action == "simulate":
        code = payload.get("simulate")
        if code is not None and code not in SIMULATIONS:
            return _json({"ok": False, "code": "invalid_request", "error": "ไม่รู้จักสถานะจำลองนี้"}, 400)
        tracker.set_simulation(code)
    elif action == "mutate":
        req_id = str(payload.get("reqId") or "")
        if not any(card.req_id == req_id for card in tracker.list_cards()):
            return _json({"ok": False, "code": "not_found", "error": "ไม่พบ requirement นี้"}, 404)
        tracker.mutate_content(req_id)
    elif action == "reset":
        tracker.reset()
        service = get_service()
        service._save({"intents": {}, "gate": {}})
    else:
        return _json({"ok": False, "code": "invalid_request", "error": "unknown action"}, 400)
    return _json({"ok": True, "simulate": tracker.simulation()})
