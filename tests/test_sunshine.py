import json
import subprocess
import unittest
from unittest.mock import patch

import main


class SunshineTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_waits_for_running_and_returns_success(self):
        with patch.object(main.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            response = await main.sunshine_restart()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.body)["ok"])
        script = run.call_args.args[0][-1]
        self.assertIn("SunshineService", script)
        self.assertIn("WaitForStatus('Running'", script)
        self.assertEqual(run.call_args.kwargs["timeout"], 45)

    async def test_denied_restart_is_not_reported_as_success(self):
        with patch.object(main.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "Access denied")):
            response = await main.sunshine_restart()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(json.loads(response.body)["ok"])
        self.assertIn("setup-sunshine-control.ps1", json.loads(response.body)["error"])

    def test_timeout_reports_failure(self):
        with patch.object(main.subprocess, "run", side_effect=subprocess.TimeoutExpired("powershell", 45)):
            self.assertFalse(main._restart_sunshine()["ok"])
