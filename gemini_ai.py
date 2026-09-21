"""Google Gemini Flash AI Advisor for Samsung RT34MB Telemetry Kiosk.

Provides:
- Zero-dependency REST client to Google Generative Language API (gemini-flash-latest / gemini-3.6-flash).
- Expert refrigeration engineering & culinary advisory grounded in Samsung RT34MB hardware specs.
- Dynamic telemetry context injection (current power, phase, chamber temperatures, voltage, daily energy).
- Resilient error handling and graceful offline degradation.
"""

from datetime import datetime
import json
import logging
import os
import sys
import urllib.parse

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    import urllib.request
    HAS_REQUESTS = False

logger = logging.getLogger("gemini_ai")

DEFAULT_MODEL = "gemini-flash-lite-latest"
FALLBACK_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-3.6-flash",
]
FALLBACK_MODEL = "gemini-flash-latest"
API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

SAMSUNG_RT34MB_SPECS = """
[ТЕХНИЧЕСКИЕ ХАРАКТЕРИСТИКИ АГРЕГАТА]
- Модель: Samsung RT34MB (RT34MBMS/SS/PG), двухкамерный No-Frost.
- Общий объем: 340 л (Морозильное отделение: 85 л, Холодильное отделение: 255 л).
- Система охлаждения: Full No-Frost с многопоточным обдувом Multi Flow (без инея и ручной разморозки).
- Компрессор: поршневой Samsung SK170K-T1U (хладагент R134a, заправка ~115 г).
- Конденсатор: встроен в боковые стенки корпуса (hot-wall technology). Нагрев боковых стенок до 40–50°C при активном охлаждении — абсолютно штатный режим (предотвращает конденсат на уплотнителях).
- ТЭН оттайки: трубчатый нагреватель испарителя мощностью ~155–165 Вт. Цикл оттайки: каждые 12–24 ч на 18–28 мин + пауза стекания капель 7–12 мин.
- Номинальный КРВ (коэффициент рабочего времени): 0.30–0.45 (20–25 мин работы / 35–55 мин отдыха).
- Защита: защитная пауза перезапуска компрессора (минимум 5 минут) для выравнивания давления хладагента.
"""


class GeminiFridgeAdvisor:
    """Zero-dependency Google Gemini Flash client for refrigerator engineering & advisory."""

    def __init__(self, api_key="", enabled=True, model=""):
        self.api_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        self.enabled = bool(enabled)
        self.model = (model or os.getenv("GEMINI_MODEL", "") or DEFAULT_MODEL).strip()
        self._tested_live = False

    def is_available(self) -> bool:
        """Check if Gemini service is configured and enabled."""
        return self.enabled and bool(self.api_key)

    def _build_system_prompt(self, context: dict = None) -> str:
        """Construct a specialized system instruction with live telemetry data."""
        ctx_str = ""
        if context:
            pwr = context.get("power", 0.0)
            volt = context.get("voltage", 220.0)
            state = "ОХЛАЖДЕНИЕ (компрессор включён)" if context.get("is_running") else "ПОКОЙ (компрессор выключен)"
            dur_min = int(context.get("cycle_duration_sec" if context.get("is_running") else "rest_duration_sec", 0) / 60)
            kwh_today = context.get("today_kwh", 0.0)
            rub_today = context.get("today_cost", 0.0)
            t_freezer = context.get("freezer_temp_est", -18.5)
            t_fridge = context.get("fridge_temp_est", 4.2)
            blackout_min = context.get("blackout_duration_min", 0)

            ctx_str = (
                f"\n[ТЕКУЩАЯ ЖИВАЯ ТЕЛЕМЕТРИЯ ХОЛОДИЛЬНИКА ПРЯМО СЕЙЧАС]\n"
                f"- Состояние: {state} (длится {dur_min} мин)\n"
                f"- Текущая потребляемая мощность: {pwr:.1f} Вт\n"
                f"- Напряжение в электросети: {volt:.1f} В\n"
                f"- Расчётные температуры камер: Морозилка {t_freezer:+.1f}°C, Холодильная камера {t_fridge:+.1f}°C\n"
                f"- Расход энергии за сегодня: {kwh_today:.3f} кВт⋅ч (~{rub_today:.2f} ₽)\n"
            )
            if blackout_min > 0:
                ctx_str += f"- ВНИМАНИЕ: Зафиксировано недавнее отключение питания на {blackout_min} мин!\n"

        return (
            "Ты — интеллектуальный бортовой инженер-консультант и эксперт по двухкамерному холодильнику Samsung RT34MB.\n"
            "Твоя задача — профессионально, доброжелательно и ёмко отвечать владельцу на любые вопросы по работе холодильника, "
            "системе No-Frost, температурным режимам, энергопотреблению, правилам хранения продуктов и диагностике.\n\n"
            f"{SAMSUNG_RT34MB_SPECS}\n"
            f"{ctx_str}\n"
            "ПРАВИЛА ОТВЕТА:\n"
            "1. Отвечай на чистом русском языке, дружелюбно и по делу.\n"
            "2. Форматируй ответ кратко для мобильного мессенджера Telegram (1–3 ёмких абзаца, используй уместные эмодзи 🧊⚡️💡).\n"
            "3. Опирайся на реальные ТТХ Samsung RT34MB и текущую телеметрию (если она передана выше).\n"
            "4. Если пользователь спрашивает про горячие боковые стенки — объясни, что конденсатор встроен в боковины (hot-wall), и это нормально при работе компрессора.\n"
            "5. Никогда не советуй вскрывать медные трубки контура с фреоном или лезть в электросхему под напряжением.\n"
            "6. При вопросах о сроках хранения продуктов сопоставляй с расчетными температурами в камерах."
        )

    def _call_api(self, model: str, payload: dict, timeout: int = 10) -> dict:
        """Execute REST request to Google Generative Language API."""
        url = f"{API_BASE_URL}/{model}:generateContent?key={self.api_key}"
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}

        if HAS_REQUESTS:
            resp = requests.post(url, data=body_bytes, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        else:
            req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read().decode("utf-8"))

    def ask_expert(self, question: str, telemetry_context: dict = None) -> str:
        """Send question with telemetry context to Gemini and return answer text."""
        if not self.is_available():
            return "⚠️ Модуль Gemini AI не настроен или отключён. Проверьте `gemini_api_key` в `config.json`."

        clean_question = (question or "").strip()
        if not clean_question:
            return "Задайте любой вопрос о работе холодильника Samsung RT34MB или хранении продуктов!"

        system_instruction = self._build_system_prompt(telemetry_context)
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": clean_question}]
                }
            ],
            "system_instruction": {
                "parts": [{"text": system_instruction}]
            },
            "generationConfig": {
                "temperature": 0.4,
                "maxOutputTokens": 800
            }
        }

        # Try primary model first, followed by all candidate models in fallback chain
        models_to_try = [self.model] if self.model else []
        for m in FALLBACK_MODELS:
            if m not in models_to_try:
                models_to_try.append(m)

        last_error = ""
        for mod in models_to_try:
            try:
                res_data = self._call_api(mod, payload)
                candidates = res_data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts and "text" in parts[0]:
                        return parts[0]["text"].strip()
                return "Холодильник слушает, но нейросеть вернула пустой ответ. Попробуйте переформулировать вопрос."
            except Exception as e:
                last_error = str(e)
                logger.warning("Gemini model %s failed: %s. Trying next...", mod, e)

        logger.error("All Gemini models failed: %s", last_error)
        return (
            "⚠️ <i>Сервис Gemini AI временно недоступен.</i>\n"
            "Проверьте соединение с интернетом или квоту API-ключа Google AI Studio."
        )
