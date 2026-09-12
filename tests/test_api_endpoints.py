import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    import auto_monitor
    HAS_AUTO_MONITOR = True
except ImportError:
    HAS_AUTO_MONITOR = False


class ApiEndpointsSecurityAndCorsTests(unittest.TestCase):
    def setUp(self):
        if not HAS_AUTO_MONITOR:
            self.skipTest("auto_monitor dependencies missing")
        self.client = auto_monitor.app.test_client()

    def test_public_wan_ip_blocked(self):
        res = self.client.get("/api/status", environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(res.status_code, 403)

    def test_lan_request_allowed_with_cors(self):
        res = self.client.get("/api/status", environ_base={"REMOTE_ADDR": "192.168.0.50"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("Access-Control-Allow-Origin"), "*")

    def test_loopback_allowed_with_cors(self):
        res = self.client.get("/api/status", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("Access-Control-Allow-Origin"), "*")

    def test_cors_options_preflight(self):
        res = self.client.options("/api/tariff", environ_base={"REMOTE_ADDR": "192.168.0.50"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("Access-Control-Allow-Origin"), "*")
        self.assertIn("POST", res.headers.get("Access-Control-Allow-Methods", ""))

    def test_analytics_endpoint(self):
        res = self.client.get("/api/analytics", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIsInstance(data, dict)
        self.assertIn("health_score", data)
        self.assertIn("energy", data)
        self.assertIn("defrost_health", data)
        self.assertIn("voltage_health", data)


if __name__ == "__main__":
    unittest.main()
