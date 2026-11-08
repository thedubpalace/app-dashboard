import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

import agent
from routers import API_ROUTERS, agent_proxy
from services import agent_token, app_control, app_modes, app_registry, reconciler, settings_store


def make_request(method="GET", path="/api/status", query=b"", body=b"", headers=()):
    scope = {"type": "http", "method": method, "path": path, "query_string": query,
             "headers": [(k.encode(), v.encode()) for k, v in headers]}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class TokenTests(unittest.TestCase):
    def test_only_the_exact_bearer_token_matches(self):
        self.assertTrue(agent_token.matches("s3cret", "Bearer s3cret"))
        self.assertTrue(agent_token.matches("s3cret", "bearer s3cret"))
        for header in (None, "", "s3cret", "Bearer wrong", "Basic s3cret", "Bearer s3cret2"):
            with self.subTest(header=header):
                self.assertFalse(agent_token.matches("s3cret", header))

    def test_env_token_wins_and_file_is_created_once(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / ".agent-token"
            with patch.object(agent_token, "TOKEN_FILE", token_file), patch.dict("os.environ", {}, clear=True):
                created = agent_token.load_or_create()
                self.assertEqual(token_file.read_text(), created)
                self.assertEqual(agent_token.load_or_create(), created)
            with patch.object(agent_token, "TOKEN_FILE", token_file), \
                    patch.dict("os.environ", {"APP_AGENT_TOKEN": "from-env"}):
                self.assertEqual(agent_token.load(), "from-env")

    def test_agent_rejects_requests_without_its_token(self):
        with self.assertRaises(HTTPException) as caught:
            agent.require_token(None)
        self.assertEqual(caught.exception.status_code, 401)
        agent.require_token(f"Bearer {agent.TOKEN}")

    def test_every_agent_api_route_requires_the_token(self):
        included = [r.include_context for r in agent.app.router.routes if hasattr(r, "include_context")]
        self.assertEqual(len(included), len(API_ROUTERS))
        for context in included:
            self.assertIn(agent.require_token, [d.dependency for d in context.dependencies])


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    def relay(self, token="tok"):
        router = agent_proxy.build_router("http://agent.test:8766/", token)
        return router.routes[0].endpoint

    async def test_relays_method_query_body_and_adds_token(self):
        request = make_request("POST", "/api/apps/demo/mode", b"x=1", b'{"mode":"dev"}',
                               [("content-type", "application/json"), ("cookie", "session=abc")])
        with patch.object(agent_proxy, "_send", return_value=(200, b'{"ok":true}', "application/json")) as send:
            response = await self.relay()("apps/demo/mode", request)
        url, method, body, headers = send.call_args.args
        self.assertEqual(url, "http://agent.test:8766/api/apps/demo/mode?x=1")
        self.assertEqual(method, "POST")
        self.assertEqual(body, b'{"mode":"dev"}')
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertNotIn("cookie", headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {"ok": True})

    async def test_agent_errors_pass_through(self):
        with patch.object(agent_proxy, "_send", return_value=(404, b'{"ok":false,"error":"unknown app: x"}', "application/json")):
            response = await self.relay()("apps/x/start", make_request("POST"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.body)["error"], "unknown app: x")

    async def test_unreachable_agent_reports_offline(self):
        with patch.object(agent_proxy, "_send", side_effect=urllib.error.URLError(ConnectionRefusedError())):
            response = await self.relay()("status", make_request())
        data = json.loads(response.body)
        self.assertEqual(response.status_code, 502)
        self.assertTrue(data["agentOffline"])
        self.assertIn("http://agent.test:8766", data["error"])

    async def test_rejected_token_explains_the_fix(self):
        with patch.object(agent_proxy, "_send", return_value=(401, b'{"detail":"no"}', "application/json")):
            response = await self.relay()("status", make_request())
        self.assertEqual(response.status_code, 401)
        self.assertIn("APP_AGENT_TOKEN", json.loads(response.body)["error"])


class ReconcilerTests(unittest.TestCase):
    def setUp(self):
        reconciler._last_attempt.clear()

    def run_reconcile(self, apps, settings, now=1000.0):
        with patch.object(settings_store, "load", return_value=settings), \
                patch.object(app_registry, "list_apps", return_value=apps), \
                patch.object(app_modes, "attach_run_modes"), \
                patch.object(app_registry, "get_app", side_effect=lambda n: {"name": n}), \
                patch.object(app_control, "start", return_value=({"ok": True}, 200)) as start:
            attempted = reconciler.reconcile_apps(now)
        return attempted, start

    def test_only_stopped_desired_monitorable_apps_are_started(self):
        settings = {"apps": {"down": {"desiredRunning": True}, "up": {"desiredRunning": True},
                             "cli": {"desiredRunning": True}, "manual": {"desiredRunning": False}, "plain": {}}}
        apps = [
            {"name": "down", "status": "stopped", "monitorable": True},
            {"name": "up", "status": "running", "monitorable": True},
            {"name": "cli", "status": "stopped", "monitorable": False},
            {"name": "manual", "status": "stopped", "monitorable": True},
            {"name": "plain", "status": "stopped", "monitorable": True},
        ]
        attempted, start = self.run_reconcile(apps, settings)
        self.assertEqual(attempted, ["down"])
        start.assert_called_once_with({"name": "down"})

    def test_failing_app_is_retried_only_after_cooldown(self):
        settings = {"apps": {"down": {"desiredRunning": True}}}
        apps = [{"name": "down", "status": "stopped", "monitorable": True}]
        self.assertEqual(self.run_reconcile(apps, settings, 1000.0)[0], ["down"])
        self.assertEqual(self.run_reconcile(apps, settings, 1000.0 + 60)[0], [])
        self.assertEqual(self.run_reconcile(apps, settings, 1000.0 + reconciler.APP_RETRY_COOLDOWN)[0], ["down"])

    def test_nothing_is_scanned_without_desired_apps(self):
        with patch.object(settings_store, "load", return_value={"apps": {"a": {}}}), \
                patch.object(app_registry, "list_apps") as list_apps:
            self.assertEqual(reconciler.reconcile_apps(), [])
        list_apps.assert_not_called()


class DesiredRunningTests(unittest.TestCase):
    def test_start_and_stop_only_update_apps_that_opted_in(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "settings.json"
            settings_file.write_text(json.dumps({"apps": {"auto": {"desiredRunning": True}, "manual": {"mode": "dev"}}}))
            with patch.object(settings_store, "SETTINGS_FILE", settings_file):
                settings_store.update_app_desired_running("auto", False)
                settings_store.update_app_desired_running("manual", False)
                settings_store.update_app_desired_running("missing", False)
                saved = json.loads(settings_file.read_text())
        self.assertEqual(saved["apps"], {"auto": {"desiredRunning": False}, "manual": {"mode": "dev"}})


if __name__ == "__main__":
    unittest.main()
