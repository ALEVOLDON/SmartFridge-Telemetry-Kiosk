import os
import sys
import unittest
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fridge_logic import (
    classify_cycle,
    duty_cycle,
    estimated_chamber_temps,
    fill_rest_after,
    food_safety_label,
    format_gap_str,
    is_loopback_ip,
    is_private_ip,
    merge_micro_cycles,
    parse_iso,
    parse_lan_network,
    tuya_scan_hosts,
)


class ClassifyCycleTests(unittest.TestCase):
    def test_short_high_power_is_cooling(self):
        self.assertEqual(classify_cycle(160, 300, 20000), "cooling")

    def test_long_high_power_is_cooling(self):
        self.assertEqual(classify_cycle(160, 7200, 20000), "cooling")

    def test_defrost_window_without_cooling_hours_is_cooling(self):
        self.assertEqual(classify_cycle(160, 1200, 1000), "cooling")

    def test_flat_heater_curve_is_defrost(self):
        powers = [160, 161, 159, 160, 162, 160, 161, 159, 160, 161]
        self.assertEqual(classify_cycle(160, 1200, 6 * 3600, powers=powers), "defrost")

    def test_fluctuating_compressor_curve_is_cooling(self):
        powers = [118, 142, 125, 155, 130, 170, 128, 145, 120, 138]
        self.assertEqual(classify_cycle(160, 1200, 6 * 3600, powers=powers), "cooling")

    def test_power_below_heater_band_is_cooling(self):
        powers = [128] * 10
        self.assertEqual(classify_cycle(128, 1200, 6 * 3600, powers=powers), "cooling")

    def test_no_power_samples_still_defrost_in_band(self):
        self.assertEqual(classify_cycle(165, 900, 5 * 3600), "defrost")


class DutyAndRestTests(unittest.TestCase):
    def test_duty_cycle_nominal(self):
        self.assertEqual(duty_cycle(32.5 * 60, 47 * 60), 0.41)

    def test_duty_cycle_empty(self):
        self.assertIsNone(duty_cycle(0, 0))

    def test_format_gap_str(self):
        self.assertEqual(format_gap_str(0), "0 мин")
        self.assertEqual(format_gap_str(60), "1 мин")
        self.assertEqual(format_gap_str(3600), "1 ч")
        self.assertEqual(format_gap_str(5400), "1 ч 30 мин")

    def test_fill_rest_krv(self):
        end = datetime(2026, 9, 1, 10, 0, 0)
        nxt = datetime(2026, 9, 1, 10, 47, 0)
        rest_sec, rest_str, rest_start, rest_end, krv = fill_rest_after(
            end, nxt, "10:47", [], 32 * 60
        )
        self.assertEqual(rest_sec, 47 * 60)
        self.assertEqual(rest_str, "47 мин")
        self.assertEqual(rest_start, "10:00")
        self.assertEqual(rest_end, "10:47")
        self.assertEqual(krv, "0.41")

    def test_fill_rest_skips_blackout_gap(self):
        end = datetime(2026, 9, 1, 10, 0, 0)
        nxt = datetime(2026, 9, 1, 12, 0, 0)
        blackouts = [("2026-09-01T10:10:00", "2026-09-01T11:40:00", 5400, "x")]
        rest_sec, rest_str, *_ = fill_rest_after(end, nxt, "12:00", blackouts, 1800)
        self.assertEqual(rest_sec, 0)
        self.assertEqual(rest_str, "затем отключение")

    def test_fill_rest_missing_telemetry(self):
        end = datetime(2026, 9, 1, 10, 0, 0)
        nxt = datetime(2026, 9, 1, 13, 0, 0)
        rest_sec, rest_str, *_ = fill_rest_after(end, nxt, "13:00", [], 1800)
        self.assertEqual(rest_sec, 0)
        self.assertEqual(rest_str, "нет записи")


class MergeAndParseTests(unittest.TestCase):
    def test_merge_micro_split_same_type(self):
        rows = [
            ("2026-09-01T10:00:00", "2026-09-01T10:10:00", 600, 130.0, 210.0, "cooling"),
            ("2026-09-01T10:10:30", "2026-09-01T10:20:00", 570, 132.0, 212.0, "cooling"),
        ]
        merged = merge_micro_cycles(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][2], 600 + 570 + 30)
        self.assertEqual(merged[0][1], "2026-09-01T10:20:00")

    def test_merge_overlapping_and_subsumed_cycles(self):
        rows = [
            ("2026-09-21T22:09:09", "2026-09-21T22:38:42", 1772, 126.7, 189.1, "cooling"),
            ("2026-09-21T22:39:14", "2026-09-21T22:44:38", 323, 126.6, 193.0, "cooling"),
            ("2026-09-21T22:39:14", "2026-09-21T22:45:10", 355, 123.4, 190.5, "cooling"),
        ]
        merged = merge_micro_cycles(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][0], "2026-09-21T22:09:09")
        self.assertEqual(merged[0][1], "2026-09-21T22:45:10")
        self.assertGreater(merged[0][2], 2100)

    def test_does_not_merge_different_types(self):
        rows = [
            ("2026-09-01T10:00:00", "2026-09-01T10:10:00", 600, 160.0, 210.0, "defrost"),
            ("2026-09-01T10:10:30", "2026-09-01T10:20:00", 570, 130.0, 210.0, "cooling"),
        ]
        self.assertEqual(len(merge_micro_cycles(rows)), 2)

    def test_parse_iso(self):
        self.assertIsNone(parse_iso(None))
        self.assertIsNone(parse_iso("not-a-date"))
        self.assertEqual(parse_iso("2026-09-01T10:00:00"), datetime(2026, 9, 1, 10, 0, 0))


class FoodSafetyAndTempModelTests(unittest.TestCase):
    def test_food_safety_bands(self):
        self.assertIn("≤4 ч", food_safety_label(3600))
        self.assertIn("4–8 ч", food_safety_label(20000))
        self.assertIn("Длительное отключение", food_safety_label(40000))

    def test_estimated_temps_are_model_not_sensor_range_drift(self):
        fz_on, fr_on = estimated_chamber_temps(True, 0)
        fz_on2, fr_on2 = estimated_chamber_temps(True, 3600)
        self.assertLess(fz_on2, fz_on)
        self.assertLess(fr_on2, fr_on)
        fz_off, fr_off = estimated_chamber_temps(False, 3000)
        self.assertGreater(fz_off, -19.5)
        self.assertGreater(fr_off, 3.6)


class LanPolicyTests(unittest.TestCase):
    def test_loopback(self):
        self.assertTrue(is_loopback_ip("127.0.0.1"))
        self.assertTrue(is_loopback_ip("::1"))
        self.assertTrue(is_loopback_ip("::ffff:127.0.0.1"))
        self.assertFalse(is_loopback_ip("192.168.0.50"))

    def test_private_and_public(self):
        self.assertTrue(is_private_ip("192.168.0.50"))
        self.assertTrue(is_private_ip("10.0.0.8"))
        self.assertTrue(is_private_ip("100.64.1.2"))
        self.assertFalse(is_private_ip("8.8.8.8"))
        self.assertFalse(is_private_ip(""))
        self.assertFalse(is_private_ip("not-an-ip"))

    def test_lan_network_and_scan_cap(self):
        net = parse_lan_network("192.168.1.0/24")
        self.assertEqual(str(net), "192.168.1.0/24")
        hosts = tuya_scan_hosts(net)
        self.assertEqual(len(hosts), 254)
        self.assertIn("192.168.1.1", hosts)
        huge = parse_lan_network("10.0.0.0/16")
        self.assertEqual(tuya_scan_hosts(huge), [])
        self.assertEqual(str(parse_lan_network("nope")), "192.168.0.0/24")


class PagesIndexSyncTests(unittest.TestCase):
    def test_landing_and_kiosk_pages_exist(self):
        root = os.path.join(ROOT, "index.html")
        static = os.path.join(ROOT, "static", "index.html")
        self.assertTrue(os.path.exists(root), "Root index.html (landing page) must exist")
        self.assertTrue(os.path.exists(static), "Static index.html (kiosk) must exist")
        with open(root, "r", encoding="utf-8") as f:
            landing = f.read()
        self.assertIn("Samsung RT34MB", landing)
        with open(static, "r", encoding="utf-8") as f:
            kiosk = f.read()
        self.assertIn("Samsung RT34MB", kiosk)

    def test_static_assets_relative_paths(self):
        static = os.path.join(ROOT, "static", "index.html")
        ipad = os.path.join(ROOT, "static", "ipad.html")
        with open(static, "r", encoding="utf-8") as f:
            kiosk_content = f.read()
        self.assertNotIn('href="static/', kiosk_content)
        self.assertNotIn('src="static/', kiosk_content)
        with open(ipad, "r", encoding="utf-8") as f:
            ipad_content = f.read()
        self.assertNotIn('href="/static/', ipad_content)
        self.assertNotIn('src="/static/', ipad_content)
        self.assertNotIn("url('/static/", ipad_content)


if __name__ == "__main__":
    unittest.main()
