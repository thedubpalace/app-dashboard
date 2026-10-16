import unittest
from unittest.mock import patch

from services import app_registry


class PortHealthCheckTests(unittest.TestCase):
    def test_supported_targets_connect_to_expected_port(self):
        cases = [
            ("https://dashboard.example/api/control/readiness", "dashboard.example", 443),
            ("http://dashboard.example/health", "dashboard.example", 80),
            ("https://dashboard.example:8765/health", "dashboard.example", 8765),
            ("localhost:8765", "localhost", 8765),
            (8765, "localhost", 8765),
            ("8765", "localhost", 8765),
        ]
        for target, host, port in cases:
            with self.subTest(target=target), patch.object(app_registry.socket, "socket") as factory:
                factory.return_value.connect_ex.return_value = 0
                self.assertTrue(app_registry._is_running(
                    {"healthCheck": {"type": "http", "target": target}}, []
                ))
                factory.return_value.connect_ex.assert_called_once_with((host, port))
                factory.return_value.close.assert_called_once()

    def test_unreachable_https_target_is_stopped(self):
        with patch.object(app_registry.socket, "socket") as factory:
            factory.return_value.connect_ex.return_value = 10061
            self.assertFalse(app_registry._check_port("https://dashboard.example/health"))

    def test_unknown_scheme_without_port_is_stopped(self):
        self.assertFalse(app_registry._check_port("custom://dashboard.example/health"))


if __name__ == "__main__":
    unittest.main()
