"""Unit tests for TypeSafe AI integration in Samsung RT34MB Kiosk."""

import unittest
from unittest.mock import MagicMock, patch
from typesafe_ai import TypeSafeFridgeAI
from telegram_bot import FridgeTelegramBot


class TestTypeSafeFridgeAI(unittest.TestCase):
    """Test suite for TypeSafe AI helper."""

    def test_offline_fallback(self):
        """When disabled, methods should safely return default fallbacks."""
        ai = TypeSafeFridgeAI(api_key="", enabled=False)
        self.assertFalse(ai.is_available())

        intent, conf = ai.classify_intent("любой текст")
        self.assertEqual(intent, "other")
        self.assertEqual(conf, 0.0)

        diag = ai.diagnose_thermodynamics(duty_cycle=0.35)
        self.assertFalse(diag["is_ai"])
        self.assertEqual(diag["level_index"], 0)

    def test_offline_duty_cycle_levels(self):
        """Test fallback duty cycle thresholds."""
        ai = TypeSafeFridgeAI(api_key="", enabled=False)

        # Excellent <= 0.42
        self.assertEqual(ai.diagnose_thermodynamics(0.35)["level_index"], 0)
        # Normal 0.43 - 0.52
        self.assertEqual(ai.diagnose_thermodynamics(0.48)["level_index"], 1)
        # Warning 0.53 - 0.65
        self.assertEqual(ai.diagnose_thermodynamics(0.60)["level_index"], 2)
        # Critical > 0.65
        self.assertEqual(ai.diagnose_thermodynamics(0.72)["level_index"], 3)

    @patch.object(TypeSafeFridgeAI, "_call_api")
    def test_mocked_classification(self, mock_call):
        """Verify Choice parsing from TypeSafe response."""
        mock_call.return_value = {
            "answers": {
                "intent": {
                    "type": "choice",
                    "choice": "status",
                    "confidence": 0.98
                }
            }
        }

        ai = TypeSafeFridgeAI(api_key="test_key", enabled=True)
        self.assertTrue(ai.is_available())

        intent, conf = ai.classify_intent("как дела у холодильника?")
        self.assertEqual(intent, "status")
        self.assertAlmostEqual(conf, 0.98)


class TestBotTypeSafeRouting(unittest.TestCase):
    """Test Telegram Bot routing integration with TypeSafe AI."""

    def setUp(self):
        self.mock_ai = MagicMock()
        self.bot = FridgeTelegramBot(
            token="123456:FAKE_TOKEN",
            chat_id="1001",
            enabled=True,
            ai_engine=self.mock_ai
        )
        self.bot.send_message = MagicMock()
        self.bot.format_status_message = MagicMock(return_value="STATUS_OK")
        self.bot.format_today_message = MagicMock(return_value="TODAY_OK")
        self.bot.format_weekly_digest = MagicMock(return_value="WEEK_OK")
        self.bot.format_audit_message = MagicMock(return_value="AUDIT_OK")

    def test_nlp_routing_to_status(self):
        """Free-form query classified as 'status' should invoke format_status_message."""
        self.mock_ai.is_available.return_value = True
        self.mock_ai.classify_intent.return_value = ("status", 0.95)

        self.bot.handle_command("как там компрессор себя чувствует?", from_chat_id="1001")

        self.mock_ai.classify_intent.assert_called_once_with("как там компрессор себя чувствует?")
        self.bot.format_status_message.assert_called_once()
        self.bot.send_message.assert_called_once()

    def test_nlp_routing_to_today(self):
        """Query classified as 'today' should invoke format_today_message."""
        self.mock_ai.is_available.return_value = True
        self.mock_ai.classify_intent.return_value = ("today", 0.99)

        self.bot.handle_command("сколько накрутило сегодня?", from_chat_id="1001")

        self.bot.format_today_message.assert_called_once()

    def test_nlp_routing_low_confidence_fallback(self):
        """Low confidence or 'other' should show unknown command message."""
        self.mock_ai.is_available.return_value = True
        self.mock_ai.classify_intent.return_value = ("other", 0.40)

        self.bot.handle_command("привет как жизнь", from_chat_id="1001")

        self.bot.format_status_message.assert_not_called()
        self.assertIn("не совсем понял", self.bot.send_message.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
