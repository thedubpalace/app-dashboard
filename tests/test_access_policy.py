"""Regression guard for the dashboard access layer.

These tests walk the *registered* routes, so a route added later without an
access rule, or a state-changing route that becomes reachable without a
session or CSRF token, fails here.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from starlette.routing import Mount

import main
from routers import agent_proxy, auth, control
from services import access_guard, access_policy
from services.dashboard_auth import create_session, read_session
from services.temporal_client import TemporalOrchestratorClient

ENV = {
    "VEDA_DASHBOARD_SESSION_SECRET": "session-secret-for-tests",
    "VEDA_DASHBOARD_USERNAME": "thedubpalace",
    "VEDA_DASHBOARD_PASSWORD": "correct horse battery staple",
    "DASHBOARD_COOKIE_SECURE": "1",
    "DASHBOARD_LOGIN_RATE_ATTEMPTS": "3",
    "DASHBOARD_LOGIN_RATE_WINDOW_SECONDS": "900",
}


def call(app, method: str, path: str, *, cookie: str | None = None, headers: dict[str, str] | None = None, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    hdrs = [(b"host", b"testserver")]
    for key, value in (headers or {}).items():
        hdrs.append((key.lower().encode(), value.encode()))
    if cookie:
        hdrs.append((b"cookie", f"veda_dashboard_session={cookie}".encode()))
    if body is not None:
        hdrs.append((b"content-type", b"application/json"))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": hdrs, "client": ("127.0.0.1", 5000),
             "server": ("testserver", 80)}
    messages: list[dict] = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.sleep(3600)

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    response_headers: dict[str, list[str]] = {}
    for key, value in start["headers"]:
        response_headers.setdefault(key.decode().lower(), []).append(value.decode())
    try:
        data = json.loads(payload)
    except ValueError:
        data = payload.decode("utf-8", "replace")
    return start["status"], data, response_headers


def concrete(template: str) -> str:
    return (template.replace("{name}", "demo-app").replace("{req_id}", "REQ-101"))


def _walk(routes, prefix=""):
    for route in routes:
        if isinstance(route, Mount):
            continue
        included = getattr(route, "original_router", None)  # FastAPI >= 0.140 keeps included routers nested
        if included is not None:
            context = getattr(route, "include_context", None)
            yield from _walk(included.routes, prefix + (getattr(context, "prefix", "") or ""))
        elif getattr(route, "methods", None):
            yield route, prefix + route.path


def app_routes(app) -> set[tuple[str, str]]:
    found = set()
    for route, path in _walk(app.routes):
        if path in {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}:
            continue
        for method in route.methods:
            if method != "HEAD":
                found.add((method, path))
    self_check = {path for _, path in found}
    assert "/api/apps" in self_check, "route walk found no API routes"
    return found


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = Path(self.tmp.name) / "audit.jsonl"
        self.security = Path(self.tmp.name) / "security.log"
        env = {**ENV, "DASHBOARD_AUDIT_LOG": str(self.audit), "DASHBOARD_SECURITY_LOG": str(self.security)}
        self.patcher = patch.dict(os.environ, env)
        self.patcher.start()
        os.environ.pop("DASHBOARD_USER_ROLES", None)
        auth._login_failures.clear()

    def tearDown(self):
        self.patcher.stop()
        auth._login_failures.clear()
        self.tmp.cleanup()

    def session(self, username="thedubpalace"):
        return create_session(username)

    def lines(self, path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class RouteCoverageTests(GuardTestCase):
    def test_every_registered_route_has_exactly_one_access_rule(self):
        rules = {(rule.method, rule.template) for rule in access_policy.RULES}
        routes = app_routes(main.app)
        self.assertEqual(sorted(routes - rules), [], "routes without an access rule")
        self.assertEqual(sorted(rules - routes), [], "rules for routes that do not exist")
        self.assertEqual(len(rules), len(access_policy.RULES), "duplicate rules")

    def test_agent_api_routes_are_all_covered(self):
        # UI-only mode relays /api/* to the agent; the guard must know every agent route too.
        from routers import API_ROUTERS
        rules = {(rule.method, rule.template) for rule in access_policy.RULES}
        for router in API_ROUTERS:
            for route in router.routes:
                for method in route.methods:
                    self.assertIn((method, route.path), rules)

    def test_public_rules_never_change_state_except_login(self):
        public = [(r.method, r.template) for r in access_policy.RULES if r.permission == access_policy.PUBLIC]
        self.assertEqual([item for item in public if item[0] != "GET"], [("POST", "/api/login")])
        self.assertEqual(sorted(t for m, t in public if m == "GET"), ["/", "/api/control/readiness", "/api/session", "/review"])


class StateChangingRouteTests(GuardTestCase):
    """Every mutation: no session -> 401; session without/with wrong CSRF -> 403; foreign Origin -> 403."""

    def mutation_rules(self):
        rules = [rule for rule in access_policy.RULES if rule.mutation and rule.permission != access_policy.PUBLIC]
        self.assertGreaterEqual(len(rules), 23)
        return rules

    def test_no_session_is_401_for_every_state_changing_route(self):
        for rule in self.mutation_rules():
            status, data, _ = call(main.app, rule.method, concrete(rule.template), body={})
            self.assertEqual(status, 401, rule.template)
            self.assertEqual(data["code"], "unauthenticated")
        self.assertEqual(self.lines(self.audit), [], "denied requests must not reach the audit/handler stage")

    def test_tampered_or_expired_cookie_is_401_session_expired(self):
        cookie, csrf = self.session()
        bad = cookie.replace("thedubpalace", "mallory", 1)
        for rule in self.mutation_rules():
            status, data, _ = call(main.app, rule.method, concrete(rule.template), cookie=bad, headers={"X-CSRF-Token": csrf}, body={})
            self.assertEqual((status, data["code"]), (401, "session_expired"), rule.template)

    def test_missing_csrf_is_403_for_every_state_changing_route(self):
        cookie, _ = self.session()
        for rule in self.mutation_rules():
            status, data, _ = call(main.app, rule.method, concrete(rule.template), cookie=cookie, body={})
            self.assertEqual((status, data["code"]), (403, "csrf_invalid"), rule.template)

    def test_wrong_csrf_is_403_for_every_state_changing_route(self):
        cookie, _ = self.session()
        _, other_csrf = self.session()
        for rule in self.mutation_rules():
            status, data, _ = call(main.app, rule.method, concrete(rule.template), cookie=cookie, headers={"X-CSRF-Token": other_csrf}, body={})
            self.assertEqual((status, data["code"]), (403, "csrf_invalid"), rule.template)

    def test_foreign_origin_is_403_even_with_valid_csrf(self):
        cookie, csrf = self.session()
        for rule in self.mutation_rules():
            status, data, _ = call(main.app, rule.method, concrete(rule.template), cookie=cookie,
                                   headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"}, body={})
            self.assertEqual((status, data["code"]), (403, "origin_mismatch"), rule.template)

    def test_denials_are_written_to_the_security_log(self):
        call(main.app, "POST", "/api/system/restart")
        entries = self.lines(self.security)
        self.assertEqual(entries[-1]["status"], 401)
        self.assertEqual(entries[-1]["permission"], access_policy.SYSTEM_ADMIN)

    def test_dangerous_handlers_are_never_called_without_a_session(self):
        with patch("services.system_service.restart_machine") as restart, \
                patch("services.agent_sessions.open_claude") as claude, \
                patch("services.agent_sessions.open_codex") as codex, \
                patch("services.docker_service.stop") as docker_stop, \
                patch("services.system_service.toggle_tailscale") as tailscale:
            for path in ("/api/system/restart", "/api/apps/demo-app/shell", "/api/apps/demo-app/codex",
                         "/api/docker/demo/stop", "/api/tailscale/toggle"):
                status, _, _ = call(main.app, "POST", path)
                self.assertEqual(status, 401, path)
        for mock in (restart, claude, codex, docker_stop, tailscale):
            mock.assert_not_called()


class ReadRouteTests(GuardTestCase):
    def test_host_inventory_reads_need_a_session(self):
        for rule in access_policy.RULES:
            if rule.method == "GET" and rule.permission != access_policy.PUBLIC:
                status, data, _ = call(main.app, "GET", concrete(rule.template))
                self.assertEqual(status, 401, rule.template)

    def test_page_shells_are_public_and_carry_the_watchdog_title(self):
        status, body, headers = call(main.app, "GET", "/")
        self.assertEqual(status, 200)
        self.assertRegex(body, r"<title>\s*App Dashboard\s*</title>")
        self.assertIn('id="authGate"', body)
        self.assertEqual(headers["x-frame-options"], ["DENY"])
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"][0])
        status, body, _ = call(main.app, "GET", "/review")
        self.assertEqual(status, 200)

    def test_static_files_are_public(self):
        status, _, _ = call(main.app, "GET", "/static/review.js")
        self.assertEqual(status, 200)


class DefaultDenyTests(GuardTestCase):
    def test_unknown_routes_are_denied_even_for_admin(self):
        cookie, csrf = self.session()
        for method, path in (("GET", "/api/unknown"), ("POST", "/api/apps/demo-app/delete"), ("GET", "/docs"),
                             ("GET", "/openapi.json"), ("DELETE", "/api/apps/demo-app"), ("PUT", "/api/settings")):
            status, data, _ = call(main.app, method, path, cookie=cookie, headers={"X-CSRF-Token": csrf})
            self.assertEqual((status, data["code"]), (403, "not_allowed"), path)

    def test_unlisted_route_on_a_new_router_is_denied(self):
        app = FastAPI()
        access_guard.install(app)

        @app.post("/api/new-feature")
        async def new_feature():  # pragma: no cover - must never run
            raise AssertionError("handler reached")

        cookie, csrf = self.session()
        status, data, _ = call(app, "POST", "/api/new-feature", cookie=cookie, headers={"X-CSRF-Token": csrf})
        self.assertEqual((status, data["code"]), (403, "not_allowed"))

    def test_agent_relay_mode_is_guarded_by_real_path(self):
        app = FastAPI()
        access_guard.install(app)
        app.include_router(auth.router)
        app.include_router(agent_proxy.build_router("http://127.0.0.1:9", "agent-token"))
        with patch("routers.agent_proxy._send") as send:
            self.assertEqual(call(app, "POST", "/api/apps/demo-app/start")[0], 401)
            self.assertEqual(call(app, "GET", "/api/apps")[0], 401)
            cookie, csrf = self.session()
            self.assertEqual(call(app, "POST", "/api/apps/demo-app/start", cookie=cookie)[0], 403)
            self.assertEqual(call(app, "GET", "/api/agent-only-secret", cookie=cookie)[0], 403)
            send.assert_not_called()
            send.return_value = (200, b"[]", "application/json")
            self.assertEqual(call(app, "GET", "/api/apps", cookie=cookie)[0], 200)
            send.assert_called_once()


class RoleTests(GuardTestCase):
    def test_viewer_can_read_but_not_change_state(self):
        os.environ["DASHBOARD_USER_ROLES"] = "thedubpalace:admin,visitor:viewer"
        cookie, csrf = self.session("visitor")
        with patch("services.app_registry.snapshot_processes", return_value=[]), patch("services.app_registry.list_apps", return_value=[]):
            status, _, _ = call(main.app, "GET", "/api/apps", cookie=cookie)
        self.assertEqual(status, 200)
        for path in ("/api/apps/demo-app/start", "/api/docker/demo/stop", "/api/system/restart", "/api/apps/demo-app/shell"):
            status, data, _ = call(main.app, "POST", path, cookie=cookie, headers={"X-CSRF-Token": csrf}, body={})
            self.assertEqual((status, data["code"]), (403, "forbidden"), path)

    def test_operator_cannot_open_terminals_or_reboot(self):
        os.environ["DASHBOARD_USER_ROLES"] = "thedubpalace:admin,ops:operator"
        cookie, csrf = self.session("ops")
        for path in ("/api/apps/demo-app/shell", "/api/apps/demo-app/codex", "/api/apps/demo-app/claude-sessions/kill-all",
                     "/api/system/restart", "/api/system/sunshine/restart", "/api/docker/fix", "/api/tailscale/toggle"):
            status, data, _ = call(main.app, "POST", path, cookie=cookie, headers={"X-CSRF-Token": csrf})
            self.assertEqual((status, data["code"]), (403, "forbidden"), path)

    def test_unknown_user_defaults_to_viewer(self):
        cookie, csrf = self.session("someone-else")
        status, data, _ = call(main.app, "POST", "/api/apps/demo-app/stop", cookie=cookie, headers={"X-CSRF-Token": csrf})
        self.assertEqual((status, data["code"]), (403, "forbidden"))

    def test_role_table_is_monotonic(self):
        viewer, operator, admin = (access_policy.ROLE_PERMISSIONS[r] for r in ("viewer", "operator", "admin"))
        self.assertTrue(viewer < operator < admin)


class AuditTests(GuardTestCase):
    def test_authorised_mutation_writes_requested_and_result(self):
        cookie, csrf = self.session()
        status, data, _ = call(main.app, "PATCH", "/api/settings/docker/filter", cookie=cookie, headers={"X-CSRF-Token": csrf},
                               body={"filter": "not-a-list"})
        self.assertEqual(status, 400)  # the handler ran
        entries = self.lines(self.audit)
        self.assertEqual([e["phase"] for e in entries], ["requested", "result"])
        self.assertEqual(entries[0]["actor"], "thedubpalace")
        self.assertEqual(entries[0]["role"], "admin")
        self.assertEqual(entries[1]["status"], 400)
        self.assertEqual(entries[0]["id"], entries[1]["id"])
        raw = self.audit.read_text(encoding="utf-8")
        self.assertNotIn(cookie, raw)
        self.assertNotIn(csrf, raw)

    def test_mutation_is_refused_when_audit_cannot_be_written(self):
        cookie, csrf = self.session()
        with patch("services.audit_log.audit", side_effect=OSError("disk full")), \
                patch("services.settings_store.set_docker_filter") as setter:
            status, data, _ = call(main.app, "PATCH", "/api/settings/docker/filter", cookie=cookie, headers={"X-CSRF-Token": csrf},
                                   body={"filter": ["x"]})
        self.assertEqual((status, data["code"]), (503, "audit_unavailable"))
        setter.assert_not_called()


class LoginTests(GuardTestCase):
    def test_login_sets_strict_httponly_secure_cookie_and_returns_csrf(self):
        status, data, headers = call(main.app, "POST", "/api/login", body={"username": "thedubpalace", "password": ENV["VEDA_DASHBOARD_PASSWORD"]})
        self.assertEqual(status, 200)
        self.assertTrue(data["csrfToken"])
        cookie = headers["set-cookie"][0]
        for flag in ("HttpOnly", "Secure", "SameSite=strict"):
            self.assertIn(flag.lower(), cookie.lower())
        value = cookie.split(";", 1)[0].split("=", 1)[1].strip('"')
        status, data, _ = call(main.app, "GET", "/api/session", cookie=value)
        self.assertEqual((status, data["authenticated"], data["role"]), (200, True, "admin"))

    def test_wrong_password_is_401_and_rate_limited(self):
        for _ in range(3):
            status, _, _ = call(main.app, "POST", "/api/login", body={"username": "thedubpalace", "password": "nope"})
            self.assertEqual(status, 401)
        status, _, headers = call(main.app, "POST", "/api/login", body={"username": "thedubpalace", "password": ENV["VEDA_DASHBOARD_PASSWORD"]})
        self.assertEqual(status, 429)
        self.assertIn("retry-after", headers)
        self.assertTrue(all(e["status"] in (401, 429) for e in self.lines(self.security)))
        self.assertNotIn("nope", self.security.read_text(encoding="utf-8"))

    def test_login_is_unavailable_without_configured_secrets(self):
        os.environ.pop("VEDA_DASHBOARD_PASSWORD")
        status, _, _ = call(main.app, "POST", "/api/login", body={"username": "thedubpalace", "password": ""})
        self.assertEqual(status, 503)

    def test_logout_needs_csrf_and_clears_cookie(self):
        cookie, csrf = self.session()
        self.assertEqual(call(main.app, "POST", "/api/logout", cookie=cookie)[0], 403)
        status, _, headers = call(main.app, "POST", "/api/logout", cookie=cookie, headers={"X-CSRF-Token": csrf})
        self.assertEqual(status, 200)
        self.assertIn('veda_dashboard_session=""', headers["set-cookie"][0])

    def test_session_cookie_is_signed_and_expires(self):
        value, csrf = create_session("alice")
        request = type("Request", (), {"cookies": {"veda_dashboard_session": value}})()
        self.assertEqual(read_session(request), {"username": "alice", "csrf": csrf})
        request.cookies["veda_dashboard_session"] = value.replace("alice", "mallory", 1)
        self.assertIsNone(read_session(request))


class ReadinessTests(GuardTestCase):
    def test_readiness_is_public_and_fails_closed_for_stale_worker(self):
        snapshot = {"registrySync": {"status": "ready", "resourceCount": 11}, "readiness": {"temporal": True, "worker": False, "docker": True}}
        with patch("routers.control._snapshot", return_value=snapshot):
            status, data, _ = call(main.app, "GET", "/api/control/readiness")
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertIn("worker task queue is offline", data["error"])

    def test_readiness_ok_when_sync_ready_and_worker_live(self):
        snapshot = {"registrySync": {"status": "ready"}, "readiness": {"temporal": True, "worker": True, "docker": True, "checkedAt": "x"},
                    "resources": [{"id": "secret.resource"}]}
        result = control.readiness_payload(snapshot)
        self.assertTrue(result["ok"])
        self.assertNotIn("resources", result)
        self.assertNotIn("checkedAt", result["dependencies"])

    def test_degraded_registry_sync_is_reported(self):
        result = control.readiness_payload({"registrySync": {"status": "degraded", "error": "resourceId is invalid"},
                                            "readiness": {"temporal": True, "worker": True}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "resourceId is invalid")


class _Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.data).encode()


class TemporalClientContractTests(unittest.TestCase):
    def test_machine_calls_use_bearer_token_and_signed_actor(self):
        with patch.dict(os.environ, {"DASHBOARD_SERVICE_TOKEN": "token", "DASHBOARD_ACTOR_SECRET": "secret"}):
            client = TemporalOrchestratorClient()
            captured = {}

            def fake_open(request, timeout):
                captured["request"] = request
                return _Response({"registrySync": {"status": "ready"}})

            with patch("services.temporal_client.urlopen", fake_open), patch("services.temporal_client.time.time", return_value=1_700_000_000):
                client.resources(actor="dashboard-read")
        request = captured["request"]
        self.assertEqual(request.headers["Authorization"], "Bearer token")
        expected = hmac.new(b"secret", "\n".join(("GET", "/api/machine/resources", "1700000000000", "dashboard-read", "operator",
                                                    hashlib.sha256(b"").hexdigest())).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(request.headers["X-veda-signature"], expected)

    def test_missing_machine_credentials_fail_closed(self):
        with patch.dict(os.environ, {"DASHBOARD_SERVICE_TOKEN": "", "DASHBOARD_ACTOR_SECRET": ""}):
            with patch.object(control, "client", TemporalOrchestratorClient()):
                result = control.readiness_payload(control._snapshot())
        self.assertFalse(result["ok"])
        self.assertIn("not configured", result["error"])


if __name__ == "__main__":
    unittest.main()
