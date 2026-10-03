from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import unittest
import asyncio
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

    def test_main_template_uses_row_local_actions_without_activity_panel(self):
        source = Path("templates/index.html").read_text(encoding="utf-8")
        for removed in ("Workflow Activity", 'id="workflowBody"', "Latest audit", "loadWorkflows"):
            self.assertNotIn(removed, source)
        self.assertNotIn('fetch("/api/history")', source)
        self.assertNotIn('fetch("/api/workflows")', source)
        self.assertIn('className = `action-state ${String(action.state).toLowerCase()}`', source)
        self.assertIn("View in Temporal ↗", source)
        self.assertIn("Admin console", source)

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
                payload = json.loads(response.body)
                self.assertEqual(payload["currentAction"]["state"], "Pending")
                self.assertEqual(payload["currentAction"]["resourceId"], "docker.demo")
                self.assertEqual(payload["currentAction"]["operation"], "start")
                self.assertIn("/temporal-ui/namespaces/", payload["currentAction"]["temporalUrl"])
                submit.assert_called_once_with("docker.demo", "start", "intent_test_123456", actor="alice", target_mode=None)

    def test_monitoring_reads_never_submit_workflows(self):
        snapshot = {"resources": [], "registrySync": {"status": "ready"}}
        with patch.object(main.client, "resources", return_value=snapshot), patch.object(main.client, "submit") as submit, \
             patch.object(main.veda_apps, "snapshot_processes", return_value=[]), patch.object(main.veda_apps, "list_apps", return_value=[]), \
             patch.object(main.docker_service, "list_containers", return_value={"available": True, "containers": []}), \
             patch.object(main.vmware_service, "list_vms", return_value={"available": True, "vms": []}), \
             patch.object(main.nginx_proxy, "public_urls", return_value={}):
            self.assertEqual(main._apps(), [])
            self.assertEqual(main._docker()["containers"], [])
            self.assertEqual(main._vmware()["vms"], [])
        submit.assert_not_called()

    def test_action_poll_returns_latest_compact_projection_without_submit(self):
        workflows = [
            {"resourceId": "docker.demo", "operation": "start", "status": "Succeeded", "workflowId": "new", "runId": "run-new", "updatedAt": "2026-10-03T10:00:00Z"},
            {"resourceId": "docker.demo", "operation": "stop", "status": "Failed", "workflowId": "old", "runId": "run-old", "updatedAt": "2026-10-03T09:00:00Z"},
        ]
        with patch.dict(os.environ, {"VEDA_DASHBOARD_SESSION_SECRET": "session-secret"}):
            cookie, _ = create_session("alice")
            request = self._request(cookie)
            with patch.object(main.client, "workflows", return_value=workflows), patch.object(main.client, "submit") as submit:
                response = asyncio.run(main.current_actions(request))
        payload = json.loads(response.body)
        self.assertEqual(len(payload["actions"]), 1)
        self.assertEqual(payload["actions"][0]["state"], "Done")
        self.assertIn("run-new/history", payload["actions"][0]["temporalUrl"])
        submit.assert_not_called()


class CompactActionProjectionTests(unittest.TestCase):
    def test_temporal_states_map_to_four_row_states(self):
        cases = {
            "Queued": "Pending", "Running": "Running", "Retrying": "Running",
            "Succeeded": "Done", "Rolled back": "Done", "Failed": "Failed",
            "Timed out": "Failed", "Rollback failed": "Failed", "Cancelled": "Failed",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                result = main._project_workflow({"resourceId": "docker.demo", "operation": "restart", "status": status, "workflowId": "wf-1"})
                self.assertEqual(result["state"], expected)
        rolled_back = main._project_workflow({"resourceId": "docker.demo", "operation": "restart", "status": "Rolled back", "workflowId": "wf-1"})
        self.assertEqual(rolled_back["message"], "Previous state restored")

    def test_official_temporal_link_is_server_built_and_encoded(self):
        with patch.object(main, "TEMPORAL_UI_URL", "https://example.test/temporal-ui"), patch.object(main, "TEMPORAL_NAMESPACE", "team/default"):
            url = main._workflow_url({"workflowId": "workflow/id with space", "runId": "run/id"})
        self.assertEqual(url, "https://example.test/temporal-ui/namespaces/team%2Fdefault/workflows/workflow%2Fid%20with%20space/run%2Fid/history")

    def test_rejected_request_has_no_temporal_link(self):
        self.assertIsNone(main._workflow_url({"status": "Rejected"}))


if __name__ == "__main__":
    unittest.main()
