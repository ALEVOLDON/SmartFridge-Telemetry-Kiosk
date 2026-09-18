"""Unit tests for FridgeTelegramBot and Telegram API integration."""

import unittest
from unittest.mock import MagicMock, patch
import sqlite3
import time
from datetime import datetime, timedelta

from telegram_bot import FridgeTelegramBot, DEFAULT_REPLY_KEYBOARD
from auto_monitor import app, state


class TestTelegramBot(unittest.TestCase):

    def setUp(self):
        self.mock_state = {
            "power": 125.4,
            "voltage": 218.6,
            "current": 0.58,
            "is_running": True,
            "current_mode": "cooling",
            "mode_title": "🟢 АКТИВНЫЙ РЕЖИМ: ОХЛАЖДЕНИЕ",
            "cycle_duration_sec": 1420,
            "rest_duration_sec": 0,
            "temp_freezer_model": -19.1,
            "temp_fridge_model": 3.8,
            "last_energy_reconciliation": {
                "accuracy_pct": 99.9,
                "delta_kwh": 0.001
            }
        }
        self.bot = FridgeTelegramBot(
            token="123456:TEST_TOKEN",
            chat_id="987654321",
            enabled=True,
            state_ref=self.mock_state
        )

    def test_authorization_check(self):
        """Owner chat_id is allowed; foreign chat_ids are strictly rejected."""
        self.assertTrue(self.bot.is_authorized("987654321"))
        self.assertTrue(self.bot.is_authorized(987654321))
        self.assertFalse(self.bot.is_authorized("111222333"))
        self.assertFalse(self.bot.is_authorized(""))

    def test_multi_user_authorization(self):
        """Comma, space, and semicolon-separated chat IDs are all properly authorized."""
        bot = FridgeTelegramBot(
            token="fake",
            chat_id="111, 222; 333",
            enabled=True,
            state_ref=self.mock_state
        )
        self.assertEqual(bot.chat_ids, ["111", "222", "333"])
        self.assertTrue(bot.is_authorized("111"))
        self.assertTrue(bot.is_authorized(222))
        self.assertTrue(bot.is_authorized("333"))
        self.assertFalse(bot.is_authorized("444"))

    @patch.object(FridgeTelegramBot, "send_message")
    def test_multi_user_send_alert_broadcasts(self, mock_send_message):
        """Alerts are broadcast to all whitelisted chat IDs."""
        mock_send_message.return_value = True
        bot = FridgeTelegramBot(
            token="fake",
            chat_id="111, 222",
            enabled=True,
            state_ref=self.mock_state
        )
        bot.send_alert("⚠️ Test Alert")
        self.assertEqual(mock_send_message.call_count, 2)
        called_cids = [call[1]["chat_id"] for call in mock_send_message.call_args_list]
        self.assertEqual(called_cids, ["111", "222"])

    def test_format_status_message(self):
        """Status message contains power, voltage, mode and temperatures."""
        text = self.bot.format_status_message()
        self.assertIn("Samsung RT34MB", text)
        self.assertIn("125.4 Вт", text)
        self.assertIn("218.6 В", text)
        self.assertIn("ОХЛАЖДЕНИЕ", text)
        self.assertIn("-19.1°C", text)
        self.assertIn("99.9%", text)

    def test_format_today_message(self):
        """Today summary queries SQLite and calculates energy and costs."""
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE cycles (
                id INTEGER PRIMARY KEY,
                start_time TEXT,
                end_time TEXT,
                duration_sec INTEGER,
                avg_power REAL,
                avg_voltage REAL,
                cycle_type TEXT
            )
        """)
        today = datetime.now().strftime("%Y-%m-%d")
        cur.execute("""
            INSERT INTO cycles (start_time, end_time, duration_sec, avg_power, avg_voltage, cycle_type)
            VALUES (?, ?, 1800, 120.0, 215.0, 'cooling')
        """, (f"{today}T10:00:00", f"{today}T10:30:00"))
        cur.execute("""
            INSERT INTO cycles (start_time, end_time, duration_sec, avg_power, avg_voltage, cycle_type)
            VALUES (?, ?, 1500, 155.0, 218.0, 'defrost')
        """, (f"{today}T12:00:00", f"{today}T12:25:00"))
        conn.commit()

        bot = FridgeTelegramBot(
            token="fake",
            chat_id="123",
            enabled=True,
            state_ref=self.mock_state,
            db_connect_fn=lambda: conn,
            config_fn=lambda: {"electricity_tariff": 2.5, "currency": "₽"}
        )

        text = bot.format_today_message()
        self.assertIn("Сводка за сегодня", text)
        self.assertIn("Компрессор", text)
        self.assertIn("Циклов охлаждения:", text)
        self.assertIn("1 (0.5 ч работы)", text)
        self.assertIn("Циклов оттайки:", text)
        conn.close()

    def test_format_weekly_digest(self):
        """Weekly digest calculates 7-day stats, KRV, and returns a verdict."""
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE cycles (
                id INTEGER PRIMARY KEY,
                start_time TEXT,
                end_time TEXT,
                duration_sec INTEGER,
                avg_power REAL,
                avg_voltage REAL,
                cycle_type TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE blackouts (
                id INTEGER PRIMARY KEY,
                start_time TEXT,
                end_time TEXT,
                duration_sec INTEGER,
                food_safety_status TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE energy_reconciliations (
                id INTEGER PRIMARY KEY,
                accuracy_pct REAL,
                delta_kwh REAL,
                date_str TEXT
            )
        """)
        now = datetime.now()
        for i in range(7):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            cur.execute("""
                INSERT INTO cycles (start_time, end_time, duration_sec, avg_power, avg_voltage, cycle_type)
                VALUES (?, ?, 1800, 125.0, 210.0, 'cooling')
            """, (f"{day}T08:00:00", f"{day}T08:30:00"))

        cur.execute("""
            INSERT INTO energy_reconciliations (accuracy_pct, delta_kwh, date_str)
            VALUES (99.9, 0.001, '2026-09-18')
        """)
        conn.commit()

        bot = FridgeTelegramBot(
            token="fake",
            chat_id="123",
            enabled=True,
            state_ref=self.mock_state,
            db_connect_fn=lambda: conn,
            config_fn=lambda: {"electricity_tariff": 1.94, "currency": "₽"}
        )

        digest = bot.format_weekly_digest()
        self.assertIn("ЕЖЕНЕДЕЛЬНЫЙ ДАЙДЖЕСТ", digest)
        self.assertIn("Samsung RT34MB", digest)
        self.assertIn("КРВ (Duty Cycle):", digest)
        self.assertIn("Вердикт:", digest)
        self.assertIn("99.9%", digest)
        conn.close()

    @patch.object(FridgeTelegramBot, "send_alert")
    def test_watchdog_alerts_and_cooldowns(self, mock_send_alert):
        """Watchdog alerts on under-voltage and long cycles with cooldown."""
        # 1. Under-voltage (< 185V) requires 90 seconds sustained
        state_dict = {
            "is_running": True,
            "voltage": 180.0,
            "power": 130.0,
            "cycle_duration_sec": 500,
            "current_mode": "cooling"
        }
        self.bot.state_ref = state_dict

        # First tick at t=0
        self.bot.check_watchdogs()
        self.assertEqual(mock_send_alert.call_count, 0)

        # Still low at t+30s -> no alert yet
        self.bot._low_v_start_time = time.time() - 30
        self.bot.check_watchdogs()
        self.assertEqual(mock_send_alert.call_count, 0)

        # Sustained for 95s -> alert fires!
        self.bot._low_v_start_time = time.time() - 95
        self.bot.check_watchdogs()
        self.assertEqual(mock_send_alert.call_count, 1)
        self.assertIn("НИЗКОЕ НАПРЯЖЕНИЕ", mock_send_alert.call_args[0][0])

        # Immediate next tick should observe 30-min cooldown
        self.bot.check_watchdogs()
        self.assertEqual(mock_send_alert.call_count, 1)

        # 2. Extended cooling cycle (> 3600s)
        state_dict["voltage"] = 220.0
        state_dict["cycle_duration_sec"] = 3700
        self.bot.check_watchdogs()
        self.assertEqual(mock_send_alert.call_count, 2)
        self.assertIn("ДЛИТЕЛЬНЫЙ ЦИКЛ ОХЛАЖДЕНИЯ", mock_send_alert.call_args[0][0])

    @patch.object(FridgeTelegramBot, "send_alert")
    def test_notify_blackout_resolved(self, mock_send_alert):
        """Blackout notification includes durations and food safety."""
        self.bot.notify_blackout_resolved(
            start_iso="2026-09-18T14:00:00",
            end_iso="2026-09-18T16:30:00",
            dur_sec=9000,
            safety_status="🟢 Безопасно: потери холода незначительны"
        )
        self.assertEqual(mock_send_alert.call_count, 1)
        alert_text = mock_send_alert.call_args[0][0]
        self.assertIn("ПИТАНИЕ 220В ВОССТАНОВЛЕНО!", alert_text)
        self.assertIn("2 ч 30 мин", alert_text)
        self.assertIn("Безопасно", alert_text)


class TestTelegramAPIEndpoints(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()

    @patch("auto_monitor.FridgeTelegramBot")
    def test_post_test_telegram_endpoint(self, mock_bot_class):
        """POST /api/test_telegram verifies configuration and sends test ping."""
        mock_instance = MagicMock()
        mock_instance.send_message.return_value = True
        mock_bot_class.return_value = mock_instance

        # Mock load_config with valid token and chat_id
        with patch("auto_monitor.load_config", return_value={
            "telegram_bot_token": "TEST_TOKEN_123",
            "telegram_chat_id": "999888"
        }):
            resp = self.client.post("/api/test_telegram", json={})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data.get("success"))

    def test_post_test_telegram_missing_creds(self):
        """POST /api/test_telegram returns 400 if credentials are not configured."""
        with patch("auto_monitor.load_config", return_value={
            "telegram_bot_token": "",
            "telegram_chat_id": ""
        }):
            resp = self.client.post("/api/test_telegram", json={})
            self.assertEqual(resp.status_code, 400)
            data = resp.get_json()
            self.assertFalse(data.get("success"))


if __name__ == "__main__":
    unittest.main()
