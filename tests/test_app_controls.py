import unittest
from unittest.mock import patch

import main


class AppControlTests(unittest.IsolatedAsyncioTestCase):
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
