from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import unittest
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        for forbidden in ("docker_service.start(", "docker_service.stop(", "docker_service.restart(", "vmware_service.start_vm(", "vmware_service.stop_vm("):
            self.assertNotIn(forbidden, source)

    def test_privileged_auxiliary_actions_remain_disabled(self):
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("status_code=501", source)
        self.assertIn('/api/system/restart', source)
        self.assertIn('/api/tailscale/toggle', source)

    def test_claude_and_codex_buttons_use_dedicated_interactive_action(self):
        source = Path("templates/index.html").read_text(encoding="utf-8")
        self.assertIn('auxiliaryAction(b, `/api/apps/${enc}/shell`)', source)
        self.assertIn('auxiliaryAction(b, `/api/apps/${enc}/codex`)', source)
        self.assertIn('auxiliaryAction(b, `/api/apps/${enc}/claude-sessions/kill-all`)', source)
        self.assertIn('auxiliaryAction(b, `/api/apps/${enc}/codex-sessions/kill-all`)', source)
        self.assertNotIn('openClaude.disabled = true', source)
        self.assertNotIn('openCodex.disabled = true', source)


class InteractiveToolTests(unittest.TestCase):
    def _request(self, cookie: str = "", csrf: str = ""):
        return type("Request", (), {"cookies": {"veda_dashboard_session": cookie} if cookie else {}, "headers": {"x-csrf-token": csrf, "idempotency-key": "interactive_test_123456"}})()

    def _session(self):
        cookie, csrf = create_session("alice")
        return self._request(cookie, csrf)

    def test_claude_launch_uses_registered_project_path_and_fixed_command(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"VEDA_DASHBOARD_SESSION_SECRET": "session-secret"}), \
             patch.object(main.veda_apps, "get_app", return_value={"name": "demo", "localPath": folder}), \
             patch.object(main.subprocess, "Popen") as popen:
            response = asyncio.run(main.app_lifecycle("demo", "shell", self._session()))
        self.assertEqual(response.status_code, 200)
        args = popen.call_args.args[0]
        self.assertEqual(args[:3], ["powershell", "-NoExit", "-Command"])
        self.assertEqual(args[3], f"Set-Location -LiteralPath '{str(Path(folder).resolve())}'; claude remote-control")
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_codex_launch_uses_registered_project_path_and_resolved_executable(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"VEDA_DASHBOARD_SESSION_SECRET": "session-secret"}), \
             patch.object(main.veda_apps, "get_app", return_value={"name": "demo", "localPath": folder}), \
             patch.object(main, "_find_codex_executable", return_value=r"C:\Tools\codex.exe"), \
             patch.object(main.subprocess, "Popen") as popen:
            response = asyncio.run(main.app_lifecycle("demo", "codex", self._session()))
        self.assertEqual(response.status_code, 200)
        command = popen.call_args.args[0][3]
        self.assertIn(f"Set-Location -LiteralPath '{str(Path(folder).resolve())}'", command)
        self.assertIn("& 'C:\\Tools\\codex.exe'", command)
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_interactive_launch_rejects_unknown_project_without_spawning(self):
        with patch.dict(os.environ, {"VEDA_DASHBOARD_SESSION_SECRET": "session-secret"}), \
             patch.object(main.veda_apps, "get_app", return_value=None), patch.object(main.subprocess, "Popen") as popen:
            response = asyncio.run(main.app_lifecycle("not-registered", "shell", self._session()))
        self.assertEqual(response.status_code, 404)
        popen.assert_not_called()

    def test_session_termination_is_scoped_to_registered_tool_and_path(self):
        process = MagicMock()
        process.pid = 42
        process.name.return_value = "powershell.exe"
        process.cmdline.return_value = ["powershell", "-Command", "claude remote-control"]
        process.children.return_value = []
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"VEDA_DASHBOARD_SESSION_SECRET": "session-secret"}), \
             patch.object(main.veda_apps, "get_app", return_value={"name": "demo", "localPath": folder}), \
             patch.object(main, "_find_tool_sessions", return_value=[{"pid": 42, "path": folder}]) as finder, \
             patch.object(main.psutil, "Process", return_value=process), patch.object(main.psutil, "wait_procs", return_value=([process], [])):
            response = asyncio.run(main.kill_tool_sessions("demo", "claude", self._session()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["killed"], 1)
        self.assertEqual(finder.call_args_list[0].kwargs, {"app_path": str(Path(folder).resolve())})
        self.assertEqual(finder.call_args_list[1].kwargs["app_path"], str(Path(folder).resolve()))
        self.assertEqual(finder.call_args_list[1].kwargs["processes"][0]["pid"], 42)
        process.terminate.assert_called_once_with()

    def test_interactive_mutations_require_session_and_csrf(self):
        response = asyncio.run(main.app_lifecycle("demo", "shell", self._request()))
        self.assertEqual(response.status_code, 401)
        with patch.dict(os.environ, {"VEDA_DASHBOARD_SESSION_SECRET": "session-secret"}):
            cookie, _ = create_session("alice")
            response = asyncio.run(main.app_lifecycle("demo", "codex", self._request(cookie, "wrong")))
        self.assertEqual(response.status_code, 403)

    def test_session_termination_rechecks_live_identity_before_kill(self):
        process = MagicMock(pid=42)
        process.name.return_value = "powershell.exe"
        process.cmdline.return_value = ["powershell", "-Command", "something-else"]
        with patch.object(main, "_find_tool_sessions", side_effect=[[{"pid": 42, "path": r"C:\Code\Demo"}], []]), \
             patch.object(main.psutil, "Process", return_value=process):
            killed = main._terminate_tool_sessions("claude", Path(r"C:\Code\Demo"))
        self.assertEqual(killed, 0)
        process.terminate.assert_not_called()

    def test_app_inventory_reports_path_scoped_session_counts(self):
        rows = [{"name": "demo", "localPath": r"C:\Code\Demo"}, {"name": "other", "localPath": r"C:\Code\Other"}]
        processes = [
            {"pid": 1, "name": "powershell.exe", "cmdline": ["powershell", "-Command", r"Set-Location -LiteralPath 'C:\Code\Demo'; claude remote-control"]},
            {"pid": 2, "name": "powershell.exe", "cmdline": ["powershell", "-Command", r"Set-Location -LiteralPath 'C:\Code\Demo'; & 'C:\Tools\codex.exe'"]},
        ]
        main._attach_session_counts(rows, processes)
        self.assertEqual((rows[0]["claudeSessions"], rows[0]["codexSessions"]), (1, 1))
        self.assertEqual((rows[1]["claudeSessions"], rows[1]["codexSessions"]), (0, 0))


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
                old = {"resourceId": "docker.demo", "operation": "stop", "status": "Succeeded", "workflowId": "old", "updatedAt": "2026-10-03T09:00:00Z"}
                with patch.object(main.client, "workflows", return_value=[old]):
                    polled = asyncio.run(main.current_actions(self._request(cookie)))
                self.assertEqual(json.loads(polled.body)["actions"][0]["workflowId"], "veda-demo-start")

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

    def test_exact_process_surfaces_receive_only_project_scoped_capabilities(self):
        snapshot = {
            "resources": [
                {"id": "process.skill-finder", "adapter": "process", "revision": 3, "state": "Stopped", "health": "Not configured", "surfaceRefs": [{"surface": "app", "key": "skill-finder"}], "capabilities": [{"operation": "start", "allowed": True}]},
                {"id": "process.html-hub", "adapter": "process", "revision": 2, "state": "Running", "health": "Healthy", "surfaceRefs": [{"surface": "app", "key": "html-hub"}], "capabilities": [{"operation": "stop", "allowed": True}, {"operation": "restart", "allowed": True}]},
            ],
            "registrySync": {"status": "ready"},
        }
        apps = [
            {"name": "skill-finder", "status": "stopped", "healthCheck": {"type": "http", "target": "http://localhost:5002/health"}},
            {"name": "html-hub", "status": "running", "healthCheck": {"type": "http", "target": "http://localhost:5005/health"}},
        ]
        with patch.object(main.client, "resources", return_value=snapshot), patch.object(main.veda_apps, "snapshot_processes", return_value=[]), \
             patch.object(main.veda_apps, "list_apps", return_value=apps), patch.object(main.nginx_proxy, "public_urls", return_value={}):
            rows = {row["name"]: row for row in main._apps()}
        self.assertEqual(rows["skill-finder"]["resourceId"], "process.skill-finder")
        self.assertEqual(rows["skill-finder"]["capabilities"], [{"operation": "start", "allowed": True}])
        self.assertEqual(rows["html-hub"]["resourceId"], "process.html-hub")
        self.assertEqual({item["operation"] for item in rows["html-hub"]["capabilities"]}, {"stop", "restart"})

    def test_unowned_process_conflict_is_read_only_and_never_killed_by_port(self):
        item = {"name": "skill-finder", "status": "running"}
        resource = {"id": "process.skill-finder", "adapter": "process", "state": "Stopped", "capabilities": [{"operation": "stop", "allowed": True}]}
        main._decorate(item, resource)
        self.assertEqual(item["capabilities"], [])
        self.assertIn("not owned by Temporal", item["ownershipConflict"])

    def test_action_poll_returns_latest_compact_projection_without_submit(self):
        main._submission_overrides.clear()
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

    def test_rejected_submission_stays_visible_over_older_success_until_new_acceptance(self):
        main._submission_overrides.clear()
        old = {"resourceId": "process.skill-finder", "operation": "start", "status": "Succeeded", "workflowId": "old", "runId": "old-run", "updatedAt": "2026-10-03T09:00:00Z"}
        with patch.dict(os.environ, {"VEDA_DASHBOARD_SESSION_SECRET": "session-secret"}):
            cookie, csrf = create_session("alice")
            request = self._request(cookie, csrf)
            with patch.object(main.client, "submit", side_effect=main.TemporalClientError("Required worker task queue is offline", 503)):
                rejected = main._submit(request, {"id": "process.skill-finder"}, "stop")
            self.assertEqual(rejected.status_code, 503)
            self.assertEqual(json.loads(rejected.body)["currentAction"]["state"], "Failed")
            with patch.object(main.client, "workflows", return_value=[old]):
                polled = asyncio.run(main.current_actions(request))
            action = json.loads(polled.body)["actions"][0]
            self.assertEqual(action["state"], "Failed")
            self.assertIn("worker task queue is offline", action["message"])
            newer = {**old, "operation": "stop", "workflowId": "new", "runId": "new-run", "updatedAt": "2027-10-04T09:00:00Z"}
            with patch.object(main.client, "workflows", return_value=[newer]):
                reconciled = asyncio.run(main.current_actions(request))
            self.assertEqual(json.loads(reconciled.body)["actions"][0]["state"], "Done")
            self.assertNotIn("process.skill-finder", main._submission_overrides)

    def test_dashboard_readiness_fails_closed_for_stale_worker(self):
        snapshot = {"registrySync": {"status": "ready", "revision": "r1"}, "readiness": {"temporal": True, "worker": False, "docker": True}}
        with patch.object(main, "_control_snapshot", return_value=snapshot):
            result = asyncio.run(main.readiness())
        self.assertFalse(result["ok"])
        self.assertIn("worker task queue is offline", result["error"])


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
