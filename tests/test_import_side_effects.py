import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class ImportSideEffectsTests(unittest.TestCase):
    def test_import_does_not_start_poller(self):
        try:
            import auto_monitor
        except ImportError as exc:
            self.skipTest("runtime deps missing: %s" % exc)
        thread = getattr(auto_monitor, "_poller_thread", None)
        self.assertTrue(thread is None or not thread.is_alive())
