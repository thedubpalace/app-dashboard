import subprocess
import sys
import unittest
from unittest.mock import patch

import main
from services import docker_service, nginx_proxy, vmware_service
from services.process_window import hidden_startupinfo


@unittest.skipUnless(sys.platform == "win32", "Windows process window behavior")
class ProcessWindowTests(unittest.TestCase):
    def assert_hidden(self, call):
        options = call.kwargs
        self.assertTrue(options["creationflags"] & subprocess.CREATE_NO_WINDOW)
        info = options["startupinfo"]
        self.assertTrue(info.dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(info.wShowWindow, subprocess.SW_HIDE)

    def test_polling_and_controls_hide_child_windows(self):
        commands = [
            lambda: docker_service.container_states(),
            lambda: docker_service.start("example"),
            lambda: docker_service.stop("example"),
            lambda: vmware_service._run("vmrun.exe", ["list"]),
            lambda: vmware_service._run("vmrun.exe", ["start", "example.vmx", "nogui"]),
            lambda: nginx_proxy._read_confs(),
            lambda: main._tailscale_status(),
            lambda: main._restart_sunshine(),
        ]
        for command in commands:
            with self.subTest(command=command), patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "{}", ""),
            ) as run:
                command()
                self.assert_hidden(run.call_args)

    def test_local_app_start_hides_window(self):
        with patch("subprocess.Popen") as popen:
            self.assertIsNone(main._start_local({"runCmd": "python example.py"}))
            self.assert_hidden(popen.call_args)

    def test_real_background_child_has_no_console(self):
        result = subprocess.run(
            [sys.executable, "-c",
             "import ctypes; print(bool(ctypes.windll.kernel32.GetConsoleWindow()))"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
            startupinfo=hidden_startupinfo(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")
