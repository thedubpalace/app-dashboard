import unittest
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import main


class AppControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_dev_restart_uses_dashboard_python_environment(self):
        entry = {"name": "planner", "runCmd": "npm run dev", "localPath": "C:/apps/planner", "healthCheck": {"type": "http", "target": "http://localhost:3000/project"}}
        with patch.object(main.app_registry, "get_app", return_value=entry), \
                patch.object(main, "_load_settings", return_value={}), \
                patch.object(main.subprocess, "Popen") as popen:
            response = await main.app_restart("planner")
        self.assertEqual(response.status_code, 200)
        command = popen.call_args.args[0]
        self.assertEqual(command[:2], [main.sys.executable, "-c"])
        compile(command[2], '<restart>', 'exec')

    async def test_compose_start_restores_dependencies(self):
        entry = {"name": "temporal", "containerName": "temporal-app", "localPath": "C:/apps/temporal"}
        settings = {"apps": {"temporal": {"dockerStartCmd": "docker compose up -d"}}}
        with patch.object(main.app_registry, "get_app", return_value=entry), \
                patch.object(main, "_load_settings", return_value=settings), \
                patch.object(main.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                patch.object(main.docker_service, "start") as start:
            response = await main.app_start("temporal")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(run.call_args.args[0], ["docker", "compose", "up", "-d"])
        self.assertEqual(run.call_args.kwargs["cwd"], entry["localPath"])
        start.assert_not_called()

    def test_compose_failure_is_reported(self):
        settings = {"apps": {"temporal": {"dockerStartCmd": "docker compose up -d"}}}
        with patch.object(main, "_load_settings", return_value=settings), \
                patch.object(main.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "database failed")):
            result = main._start_container({"name": "temporal"}, "temporal-app")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "database failed")

    def test_cli_stop_does_not_stop_other_projects(self):
        project = str(Path("project-a").resolve())
        own = Mock(info={"name": "python.exe", "cwd": project})
        other = Mock(info={"name": "python.exe", "cwd": str(Path("project-b").resolve())})
        with patch.object(main.psutil, "process_iter", return_value=[own, other]):
            self.assertEqual(main._kill_by_name("python", project), 1)
        own.terminate.assert_called_once()
        other.terminate.assert_not_called()

    async def test_container_only_start_and_stop_use_docker(self):
        entry = {
            "name": "solar-system",
            "containerName": "solar-system",
            "runCmd": "docker start solar-system",
        }
        for action, handler in (("start", main.app_start), ("stop", main.app_stop)):
            with self.subTest(action=action), \
                    patch.object(main.app_registry, "get_app", return_value=entry), \
                    patch.object(main, "_load_settings", return_value={}), \
                    patch.object(main.docker_service, action, return_value={"ok": True}) as control, \
                    patch.object(main, "_start_local") as start_local, \
                    patch.object(main, "_stop_local") as stop_local:
                response = await handler("solar-system")
                self.assertEqual(response.status_code, 200)
                control.assert_called_once_with("solar-system")
                start_local.assert_not_called()
                stop_local.assert_not_called()

    async def test_dev_mode_start_and_stop_stay_local(self):
        entry = {"name": "example", "containerName": "example", "runCmd": "npm run dev"}
        settings = {"apps": {"example": {"mode": "dev"}}}
        for action, handler, helper, result in (
            ("start", main.app_start, "_start_local", None),
            ("stop", main.app_stop, "_stop_local", 1),
        ):
            with self.subTest(action=action), \
                    patch.object(main.app_registry, "get_app", return_value=entry), \
                    patch.object(main, "_load_settings", return_value=settings), \
                    patch.object(main.docker_service, action) as control, \
                    patch.object(main, helper, return_value=result) as local:
                response = await handler("example")
                self.assertEqual(response.status_code, 200)
                local.assert_called_once_with(entry)
                control.assert_not_called()
