import os
import sys
import unittest
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    import auto_monitor
    HAS_AUTO_MONITOR = True
except ImportError:
    HAS_AUTO_MONITOR = False


class EnergyReconciliationTests(unittest.TestCase):
    def setUp(self):
        if not HAS_AUTO_MONITOR:
            self.skipTest("auto_monitor dependencies missing")
        self.client = auto_monitor.app.test_client()

    def test_reconcile_energy_get_endpoint_lan(self):
        res = self.client.get("/api/reconcile-energy", environ_base={"REMOTE_ADDR": "192.168.0.50"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("Access-Control-Allow-Origin"), "*")
        data = res.get_json()
        self.assertIsInstance(data, dict)
        self.assertIn("latest", data)
        self.assertIn("history", data)

    def test_reconcile_energy_wan_blocked(self):
        res = self.client.get("/api/reconcile-energy", environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(res.status_code, 403)
        res_post = self.client.post("/api/reconcile-energy", environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(res_post.status_code, 403)

    def test_reconcile_energy_options_cors(self):
        res = self.client.options("/api/reconcile-energy", environ_base={"REMOTE_ADDR": "192.168.0.50"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("Access-Control-Allow-Origin"), "*")

    def test_analytics_includes_reconciliation(self):
        res = self.client.get("/api/analytics?refresh=1", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("energy", data)
        self.assertIn("reconciliation", data["energy"])

    def test_accuracy_math(self):
        local_kwh = 0.929
        cloud_kwh = 0.922
        delta = round(abs(local_kwh - cloud_kwh), 3)
        base = max(cloud_kwh, local_kwh, 0.001)
        acc = round(max(0.0, 100.0 - (delta / base * 100.0)), 1)
        self.assertEqual(delta, 0.007)
        self.assertGreaterEqual(acc, 99.0)


if __name__ == "__main__":
    unittest.main()
