"""Unit tests for GeminiFridgeAdvisor and Telegram Bot Gemini integration."""

import unittest
from unittest.mock import MagicMock, patch
from gemini_ai import GeminiFridgeAdvisor
from telegram_bot import FridgeTelegramBot


class TestGeminiFridgeAdvisor(unittest.TestCase):
    """Test suite for Gemini AI helper."""

    def test_availability(self):
        # Disabled
        adv_disabled = GeminiFridgeAdvisor(api_key="TEST_KEY", enabled=False)
        self.assertFalse(adv_disabled.is_available())

        # No key
        adv_no_key = GeminiFridgeAdvisor(api_key="", enabled=True)
        self.assertFalse(adv_no_key.is_available())

        # Enabled with key
        adv_ok = GeminiFridgeAdvisor(api_key="TEST_KEY", enabled=True)
        self.assertTrue(adv_ok.is_available())

    def test_build_system_prompt_without_context(self):
        adv = GeminiFridgeAdvisor(api_key="TEST_KEY")
        prompt = adv._build_system_prompt(None)
        self.assertIn("Samsung RT34MB", prompt)
        self.assertIn("No-Frost", prompt)
        self.assertIn("SK170K-T1U", prompt)
        self.assertIn("hot-wall", prompt)

    def test_build_system_prompt_with_live_context(self):
        adv = GeminiFridgeAdvisor(api_key="TEST_KEY")
        ctx = {
            "power": 145.2,
            "voltage": 221.5,
            "is_running": True,
            "cycle_duration_sec": 1800,
            "today_kwh": 1.25,
            "today_cost": 2.45,
            "freezer_temp_est": -19.5,
            "fridge_temp_est": 3.9,
            "blackout_duration_min": 15
        }
        prompt = adv._build_system_prompt(ctx)
        self.assertIn("145.2 Вт", prompt)
        self.assertIn("221.5 В", prompt)
        self.assertIn("-19.5°C", prompt)
        self.assertIn("15 мин", prompt)

    @patch.object(GeminiFridgeAdvisor, "_call_api")
    def test_ask_expert_success(self, mock_call):
        mock_call.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "Нагрев боковых стенок Samsung RT34MB — абсолютно нормален!"}
                        ]
                    }
                }
            ]
        }
        adv = GeminiFridgeAdvisor(api_key="TEST_KEY", enabled=True)
        res = adv.ask_expert("Почему горячие бока?")
        self.assertIn("абсолютно нормален", res)

    @patch.object(GeminiFridgeAdvisor, "_call_api")
    def test_ask_expert_graceful_failure(self, mock_call):
        mock_call.side_effect = Exception("Simulated connection timeout")
        adv = GeminiFridgeAdvisor(api_key="TEST_KEY", enabled=True)
        res = adv.ask_expert("Тестовый вопрос")
        self.assertIn("временно недоступен", res)

    def test_bot_routes_unhandled_query_to_gemini(self):
        mock_gemini = MagicMock()
        mock_gemini.is_available.return_value = True
        mock_gemini.ask_expert.return_value = "Совет от Gemini: храните молоко на средней полке."

        bot = FridgeTelegramBot(
            token="MOCK_TOKEN",
            chat_id="12345",
            enabled=True,
            gemini_engine=mock_gemini
        )

        with patch.object(bot, "send_message") as mock_send, \
             patch.object(bot, "send_chat_action") as mock_action:
            bot.handle_command("Где лучше хранить сыр и молоко?", "12345")
            mock_action.assert_called_with("typing", chat_id="12345")
            mock_gemini.ask_expert.assert_called_once()
            mock_send.assert_called_with(
                "Совет от Gemini: храните молоко на средней полке.",
                chat_id="12345",
                reply_markup=unittest.mock.ANY
            )


if __name__ == "__main__":
    unittest.main()
