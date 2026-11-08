import unittest
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import sys

from routers import apps
from services import app_control, app_registry, docker_service, local_process, settings_store


class AppControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_dev_restart_uses_dashboard_python_environment(self):
        entry = {"name": "planner", "runCmd": "npm run dev", "localPath": "C:/apps/planner", "healthCheck": {"type": "http", "target": "http://localhost:3000/project"}}
        with patch.object(app_registry, "get_app", return_value=entry), \
                patch.object(settings_store, "load", return_value={}), \
                patch.object(subprocess, "Popen") as popen:
            response = await apps.app_restart("planner")
        self.assertEqual(response.status_code, 200)
        command = popen.call_args.args[0]
        self.assertEqual(command[:2], [sys.executable, "-c"])
        compile(command[2], '<restart>', 'exec')

    async def test_compose_start_restores_dependencies(self):
        entry = {"name": "temporal", "containerName": "temporal-app", "localPath": "C:/apps/temporal"}
        settings = {"apps": {"temporal": {"dockerStartCmd": "docker compose up -d"}}}
        with patch.object(app_registry, "get_app", return_value=entry), \
                patch.object(settings_store, "load", return_value=settings), \
                patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                patch.object(docker_service, "start") as start:
            response = await apps.app_start("temporal")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(run.call_args.args[0], ["docker", "compose", "up", "-d"])
        self.assertEqual(run.call_args.kwargs["cwd"], entry["localPath"])
        start.assert_not_called()

    def test_compose_failure_is_reported(self):
        settings = {"apps": {"temporal": {"dockerStartCmd": "docker compose up -d"}}}
        with patch.object(settings_store, "load", return_value=settings), \
                patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "database failed")):
            result = app_control.start_container({"name": "temporal"}, "temporal-app")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "database failed")

    def test_cli_stop_does_not_stop_other_projects(self):
        project = str(Path("project-a").resolve())
        own = Mock(info={"name": "python.exe", "cwd": project})
        other = Mock(info={"name": "python.exe", "cwd": str(Path("project-b").resolve())})
        with patch.object(local_process.psutil, "process_iter", return_value=[own, other]):
            self.assertEqual(local_process.kill_by_name("python", project), 1)
        own.terminate.assert_called_once()
        other.terminate.assert_not_called()

    async def test_container_only_start_and_stop_use_docker(self):
        entry = {
            "name": "solar-system",
            "containerName": "solar-system",
            "runCmd": "docker start solar-system",
        }
        for action, handler in (("start", apps.app_start), ("stop", apps.app_stop)):
            with self.subTest(action=action), \
                    patch.object(app_registry, "get_app", return_value=entry), \
                    patch.object(settings_store, "load", return_value={}), \
                    patch.object(docker_service, action, return_value={"ok": True}) as control, \
                    patch.object(app_control, "start_local") as start_local, \
                    patch.object(app_control, "stop_local") as stop_local:
                response = await handler("solar-system")
                self.assertEqual(response.status_code, 200)
                control.assert_called_once_with("solar-system")
                start_local.assert_not_called()
                stop_local.assert_not_called()

    async def test_dev_mode_start_and_stop_stay_local(self):
        entry = {"name": "example", "containerName": "example", "runCmd": "npm run dev"}
        settings = {"apps": {"example": {"mode": "dev"}}}
        for action, handler, helper, result in (
            ("start", apps.app_start, "start_local", None),
            ("stop", apps.app_stop, "stop_local", 1),
        ):
            with self.subTest(action=action), \
                    patch.object(app_registry, "get_app", return_value=entry), \
                    patch.object(settings_store, "load", return_value=settings), \
                    patch.object(docker_service, action) as control, \
                    patch.object(app_control, helper, return_value=result) as local:
                response = await handler("example")
                self.assertEqual(response.status_code, 200)
                local.assert_called_once_with(entry)
                control.assert_not_called()
