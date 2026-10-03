from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services.dashboard_auth import create_session, read_session
from services.temporal_client import TemporalOrchestratorClient
import main


class _Response:
    def __init__(self, data: dict):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.data).encode()


class TemporalClientContractTests(unittest.TestCase):
    def test_signed_request_covers_method_path_actor_and_body(self):
        with patch.dict(os.environ, {"DASHBOARD_SERVICE_TOKEN": "token", "DASHBOARD_ACTOR_SECRET": "secret"}):
            client = TemporalOrchestratorClient()
            captured = {}

            def fake_open(request, timeout):
                captured.update({"request": request, "timeout": timeout})
                return _Response({"ok": True, "workflowId": "operation:test"})

            with patch("services.temporal_client.urlopen", fake_open), patch("services.temporal_client.time.time", return_value=1_700_000_000):
                result = client.submit("docker.demo", "restart", "intent-12345", actor="alice")

            request = captured["request"]
            body = request.data
            body_hash = hashlib.sha256(body).hexdigest()
            expected = hmac.new(b"secret", "\n".join(("POST", "/api/machine/operations", "1700000000000", "alice", "operator", body_hash)).encode(), hashlib.sha256).hexdigest()
            self.assertEqual(request.headers["X-veda-signature"], expected)
            self.assertEqual(result["workflowId"], "operation:test")

    def test_session_cookie_is_signed_and_expires(self):
        with patch.dict(os.environ, {"VEDA_DASHBOARD_SESSION_SECRET": "session-secret"}):
            value, csrf = create_session("alice")
            request = type("Request", (), {"cookies": {"veda_dashboard_session": value}})()
            self.assertEqual(read_session(request), {"username": "alice", "csrf": csrf})
            tampered = value.replace("alice", "mallory", 1)
            request.cookies["veda_dashboard_session"] = tampered
            self.assertIsNone(read_session(request))


class SourceBoundaryTests(unittest.TestCase):
    def test_login_form_has_no_hardcoded_username(self):
        source = Path("templates/index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="loginUser" class="modal-input" autocomplete="username" placeholder="Username" value="admin"', source)
        self.assertNotRegex(source, r'id="loginUser"[^>]+value=')

    def test_login_errors_render_inside_accessible_modal_region(self):
        source = Path("templates/index.html").read_text(encoding="utf-8")
        self.assertRegex(source, r'id="loginError"[^>]+role="alert"[^>]+aria-live="assertive"')
        self.assertIn('error.classList.remove("hidden")', source)
        self.assertIn('error.textContent = ""; error.classList.add("hidden")', source)
        self.assertNotIn('if (!res.ok) return toast(data.error || "Sign in failed", false)', source)

    def test_enabled_routes_do_not_call_mutating_services(self):
        source = Path("main.py").read_text(encoding="utf-8")
        for forbidden in ("docker_service.start(", "docker_service.stop(", "docker_service.restart(", "vmware_service.start_vm(", "vmware_service.stop_vm(", "subprocess.Popen("):
            self.assertNotIn(forbidden, source)

    def test_auxiliary_actions_are_explicitly_disabled(self):
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("status_code=501", source)
        self.assertIn('/api/system/restart', source)
        self.assertIn('/api/tailscale/toggle', source)


class FacadeFailureTests(unittest.TestCase):
    def _request(self, cookie: str = "", csrf: str = ""):
        return type("Request", (), {"cookies": {"veda_dashboard_session": cookie} if cookie else {}, "headers": {"x-csrf-token": csrf, "idempotency-key": "intent_test_123456"}})()

    def test_mutation_requires_dashboard_session(self):
        response = main._submit(self._request(), {"id": "docker.demo"}, "start")
        self.assertEqual(response.status_code, 401)

    def test_unmanaged_resource_fails_closed(self):
        with patch.dict(os.environ, {"VEDA_DASHBOARD_SESSION_SECRET": "session-secret"}):
            cookie, csrf = create_session("alice")
            response = main._submit(self._request(cookie, csrf), None, "start")
            self.assertEqual(response.status_code, 422)

    def test_facade_returns_async_workflow_projection(self):
        with patch.dict(os.environ, {"VEDA_DASHBOARD_SESSION_SECRET": "session-secret"}):
            cookie, csrf = create_session("alice")
            with patch.object(main.client, "submit", return_value={"ok": True, "workflowId": "veda-demo-start", "status": "Queued"}) as submit:
                response = main._submit(self._request(cookie, csrf), {"id": "docker.demo"}, "start")
            self.assertEqual(response.status_code, 202)
            submit.assert_called_once_with("docker.demo", "start", "intent_test_123456", actor="alice", target_mode=None)


if __name__ == "__main__":
    unittest.main()
