import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which('powershell.exe'), 'Windows launcher tests')
class StartServiceTests(unittest.TestCase):
    def launch(self, runtime=None, import_exit=0, already_running=False):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / 'start-service.ps1'
            shutil.copyfile(Path(__file__).resolve().parents[1] / 'start-service.ps1', launcher)
            if runtime is not None:
                (launcher.parent / '.env.runtime').write_text(runtime, encoding='utf-8')
            environment = os.environ.copy()
            for name in ('APP_DASHBOARD_HOST', 'APP_DASHBOARD_PORT', 'VEDA_DASHBOARD_HOST', 'VEDA_DASHBOARD_PORT'):
                environment.pop(name, None)
            # Stub only Python: exercise the real PowerShell launcher without
            # starting a server or depending on a developer's environment.
            command = (
                ("function Invoke-WebRequest { [pscustomobject]@{StatusCode=200;Content='<title>App Dashboard</title>'} }; "
                 if already_running else "function Invoke-WebRequest { throw 'No server listening' }; ")
                + "function python { if ($args[0] -eq '-c') { $global:LASTEXITCODE = "
                + str(import_exit)
                + "; return }; ConvertTo-Json -Compress -InputObject @($args); $global:LASTEXITCODE = 0 }; & '"
                + str(launcher).replace("'", "''") + "'"
            )
            return subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', command],
                env=environment, capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )

    def test_missing_optional_env_uses_defaults(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ['-m', 'uvicorn', 'main:app', '--host', '0.0.0.0', '--port', 8765])

    def test_quoted_runtime_values_and_current_names_take_precedence(self):
        result = self.launch('VEDA_DASHBOARD_HOST=192.168.1.37\nAPP_DASHBOARD_HOST="127.0.0.1"\nAPP_DASHBOARD_PORT=\'9876\'\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[-4:], ['--host', '127.0.0.1', '--port', 9876])

    def test_invalid_port_fails_before_starting_python(self):
        result = self.launch('APP_DASHBOARD_PORT=70000\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must be a number from 1 to 65535', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_missing_dependencies_report_setup_instructions(self):
        result = self.launch(import_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('install requirements.txt', result.stderr)

    def test_running_dashboard_does_not_start_a_second_server(self):
        result = self.launch(already_running=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('already running at http://127.0.0.1:8765/', result.stdout)
        self.assertNotIn('uvicorn', result.stdout)
