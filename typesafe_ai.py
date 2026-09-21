"""TypeSafe AI (Jev System One) integration for Samsung RT34MB Telemetry Kiosk.

Provides:
- High-speed (System 1) Natural Language Intent Routing for Telegram Bot.
- Calibrated Thermodynamic Health Scoring for Refrigerator Duty Cycles.
- Pure Python implementation (requests / urllib) with ZERO heavy external dependencies.
- Ultra-lightweight: runs seamlessly on Android Termux (ARM), Linux, and Windows.
"""

from __future__ import annotations
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.error
    HAS_REQUESTS = False

try:
    import winreg
    HAS_WINREG = True
except ImportError:
    HAS_WINREG = False

logger = logging.getLogger("typesafe_ai")


def get_stored_api_key() -> str:
    """Retrieve TypeSafe API key from env or Windows registry."""
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key

    if HAS_WINREG:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as reg_key:
                val, _ = winreg.QueryValueEx(reg_key, "TYPESAFE_API_KEY")
                if val:
                    os.environ["TYPESAFE_API_KEY"] = str(val).strip()
                    return str(val).strip()
        except Exception:
            pass

    return ""


class TypeSafeFridgeAI:
    """Ultra-lightweight, resilient System-1 co-pilot for Samsung RT34MB Kiosk."""

    API_URL = "https://api.typesafe.ai/v1/systemone"
    MODEL = "jev-1.13.0"

    INTENT_CRITERIA = {
        "status": "Запрос текущего состояния: как там холодильник, работает или отдыхает, морозит ли сейчас, спит ли компрессор, мощность, напряжение, температура",
        "today": "Запрос статистики за сегодня: сколько накрутило сегодня, расход за день, кВт⋅ч за сутки, сколько по деньгам",
        "week": "Запрос недельного отчёта: статистика за неделю, дайджест за 7 дней, недельный расход, энергопотребление",
        "audit": "Запрос технического аудита и причин: почему так, почему гудит, проверка компрессора, диагностика, в чём причина, что случилось",
        "reconcile": "Сверка счётчика с розеткой: калибровка, сравнить с Tuya Cloud, точность измерений",
        "help": "Справка и помощь: как пользоваться, команды, что ты умеешь",
        "thanks": "Благодарность, похвала, подтверждение, одобрение: отлично, спасибо, супер, молодец, вроде работает, понял, хорошо, ок",
        "chitchat": "Приветствия, юмор, сарказм, подколы, обращение к боту: как дела, здорово, привет железяка, кто ты",
        "other": "Флуд или нераспознанный текст"
    }

    HEALTH_LEVELS = [
        "1. Отлично: КРВ <= 0.42, стабильное напряжение 220-230В, короткие циклы охлаждения",
        "2. Нормально: КРВ 0.43 - 0.52, умеренная нагрузка, штатная работа компрессора",
        "3. Внимание: КРВ 0.53 - 0.65 или частые просадки напряжения ниже 190В",
        "4. Критично: КРВ > 0.65 или непрерывная работа компрессора более 60 минут"
    ]

    HEALTH_NAMES = ["Отличное (🟢)", "Штатное (🟢)", "Внимание (🟡)", "Критическое (🔴)"]

    def __init__(self, api_key: str = "", enabled: bool = True, timeout: int = 8):
        self.api_key = api_key.strip() or get_stored_api_key()
        self.enabled = bool(enabled and self.api_key)
        self.timeout = timeout

        if self.enabled:
            logger.info("TypeSafe AI (Jev System One REST) initialized.")

    def is_available(self) -> bool:
        """Check if TypeSafe AI service has a key and is enabled."""
        return self.enabled and bool(self.api_key)

    def _call_api(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Direct REST POST to api.typesafe.ai/v1/systemone."""
        if not self.is_available():
            return None

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        data_bytes = json.dumps(payload).encode("utf-8")

        if HAS_REQUESTS:
            try:
                resp = requests.post(self.API_URL, data=data_bytes, headers=headers, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.json()
                logger.warning("TypeSafe API HTTP %s: %s", resp.status_code, resp.text)
            except Exception as e:
                logger.error("TypeSafe API requests error: %s", e)
        else:
            try:
                req = urllib.request.Request(self.API_URL, data=data_bytes, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if resp.status == 200:
                        return json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                logger.error("TypeSafe API urllib error: %s", e)

        return None

    def classify_intent(self, text: str) -> Tuple[str, float]:
        """Classify user text into a bot intent using TypeSafe Choice primitive.

        Returns:
            Tuple of (intent_name, confidence)
        """
        if not self.is_available() or not text or not text.strip():
            return "other", 0.0

        payload = {
            "model": self.MODEL,
            "state": text.strip(),
            "questions": {
                "intent": {
                    "type": "choice",
                    "instructions": "Определи намерение пользователя в боте мониторинга холодильника",
                    "criteria": self.INTENT_CRITERIA
                }
            }
        }

        res = self._call_api(payload)
        if res and "answers" in res:
            ans = res["answers"].get("intent", {})
            choice = ans.get("choice", "other")
            confidence = float(ans.get("confidence", 0.0))
            return choice, confidence

        return "other", 0.0

    def diagnose_thermodynamics(
        self,
        duty_cycle: float,
        cycle_duration_min: float = 0.0,
        avg_voltage: float = 220.0,
        power_watts: float = 0.0
    ) -> Dict[str, Any]:
        """Evaluate compressor health using Score and Noul primitives.

        Returns:
            Dict with health_score, level_index, level_name, confidence, alert_prob, is_ai
        """
        state_desc = (
            f"Телеметрия компрессора Samsung RT34MB: "
            f"КРВ (Duty Cycle) = {duty_cycle:.2f}. "
            f"Длительность цикла охлаждения: {cycle_duration_min:.1f} мин. "
            f"Среднее напряжение сети: {avg_voltage:.1f} В. "
            f"Мощность: {power_watts:.1f} Вт."
        )

        payload = {
            "model": self.MODEL,
            "state": state_desc,
            "questions": {
                "health_score": {
                    "type": "score",
                    "instructions": "Оцени состояние компрессора по 4-уровневой шкале износа",
                    "criteria": self.HEALTH_LEVELS
                },
                "alert_required": {
                    "type": "noul",
                    "instructions": "Требуется ли срочное аварийное оповещение владельца?"
                }
            }
        }

        res = self._call_api(payload)
        if res and "answers" in res:
            score_ans = res["answers"].get("health_score", {})
            alert_ans = res["answers"].get("alert_required", {})

            raw_score = float(score_ans.get("score", 1.0))
            lvl_idx = min(3, max(0, int(round(raw_score))))
            conf = float(score_ans.get("confidence", 0.8))
            alert_p = float(alert_ans.get("noul", 0.0))

            return {
                "score": raw_score,
                "level_index": lvl_idx,
                "level_name": self.HEALTH_NAMES[lvl_idx],
                "confidence": conf,
                "alert_prob": alert_p,
                "is_ai": True
            }

        # Safe algorithmic heuristic fallback if API is unreachable
        lvl = 0 if duty_cycle <= 0.42 else (1 if duty_cycle <= 0.52 else (2 if duty_cycle <= 0.65 else 3))
        return {
            "score": float(lvl),
            "level_index": lvl,
            "level_name": self.HEALTH_NAMES[lvl],
            "confidence": 0.5,
            "alert_prob": 0.0 if lvl < 2 else 0.8,
            "is_ai": False
        }
