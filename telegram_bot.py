"""Telegram Bot for Samsung RT34MB Telemetry Kiosk.

Provides:
- Real-time emergency watchdog alerts (blackouts, under-voltage < 185V, extended cooling > 60m, abnormal defrost > 35m)
- Automated weekly digest (Sunday 20:00) with energy cost, compressor health, defrost status, and voltage risks
- Interactive 2-way commands and persistent reply keyboard:
    [🟢 Статус] [📊 Отчёт за неделю]
    [⚡ За сегодня] [⚖️ Сверить счётчик]
    [🧠 Аудит] [❓ Помощь]
- Pure Python HTTP implementation using requests / urllib with zero heavy external dependencies.
"""

from datetime import datetime, timedelta
import json
import logging
import threading
import time
import urllib.parse

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    import urllib.request
    HAS_REQUESTS = False

logger = logging.getLogger("telegram_bot")

DEFAULT_REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "🟢 Статус"}, {"text": "📊 Отчёт за неделю"}],
        [{"text": "⚡ За сегодня"}, {"text": "⚖️ Сверить счётчик"}],
        [{"text": "🧠 Аудит"}, {"text": "❓ Помощь"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}


class FridgeTelegramBot:
    """Lightweight, resilient Telegram Bot runner for RT34MB Kiosk."""

    def __init__(self, token="", chat_id="", enabled=False,
                 state_ref=None, db_connect_fn=None, reconcile_fn=None, config_fn=None):
        self.token = str(token).strip()
        self.chat_id = str(chat_id).strip()
        self.enabled = bool(enabled and self.token and self.chat_id)
        self.state_ref = state_ref
        self.db_connect_fn = db_connect_fn
        self.reconcile_fn = reconcile_fn
        self.config_fn = config_fn

        self._stop_event = threading.Event()
        self._thread = None
        self._last_update_id = 0

        # Watchdog cooldowns (avoid alerting repeatedly)
        self._last_low_v_alert = 0.0
        self._low_v_start_time = None
        self._last_long_cycle_alert = 0.0
        self._last_long_defrost_alert = 0.0
        self._last_weekly_sent_date = None
        self.last_error_desc = ""

    def update_credentials(self, token, chat_id, enabled):
        """Update bot configuration at runtime."""
        self.token = str(token).strip()
        self.chat_id = str(chat_id).strip()
        self.enabled = bool(enabled and self.token and self.chat_id)

    def _api_call(self, method, payload=None, timeout=10):
        """Execute a Telegram Bot API method via HTTP POST."""
        if not self.token:
            self.last_error_desc = "Токен бота не указан"
            return None
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        headers = {"Content-Type": "application/json"}
        data = json.dumps(payload or {}).encode("utf-8")

        if HAS_REQUESTS:
            try:
                resp = requests.post(url, data=data, headers=headers, timeout=timeout)
                if resp.status_code == 200:
                    self.last_error_desc = ""
                    return resp.json()
                try:
                    err_json = resp.json()
                    self.last_error_desc = err_json.get("description", resp.text)
                except Exception:
                    self.last_error_desc = resp.text
                logger.warning("Telegram API error %s: %s", resp.status_code, resp.text)
            except Exception as e:
                self.last_error_desc = str(e)
                logger.debug("Telegram API request error: %s", e)
        else:
            try:
                import urllib.request
                import urllib.error
                req = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    self.last_error_desc = ""
                    return json.loads(response.read().decode("utf-8"))
            except Exception as e:
                try:
                    if hasattr(e, "read"):
                        err_json = json.loads(e.read().decode("utf-8"))
                        self.last_error_desc = err_json.get("description", str(e))
                    else:
                        self.last_error_desc = str(e)
                except Exception:
                    self.last_error_desc = str(e)
                logger.debug("Telegram API error: %s", e)
        return None

    def send_message(self, text, chat_id=None, reply_markup=None, parse_mode="HTML"):
        """Send a message to authorized chat."""
        target_chat = str(chat_id or self.chat_id).strip()
        if not self.token or not target_chat:
            return False

        payload = {
            "chat_id": target_chat,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        res = self._api_call("sendMessage", payload)
        return bool(res and res.get("ok"))

    def send_alert(self, text, alert_type="alert"):
        """Send a high-priority watchdog alert to the owner."""
        if not self.enabled:
            return False
        return self.send_message(text, reply_markup=DEFAULT_REPLY_KEYBOARD)

    def is_authorized(self, from_chat_id):
        """Check if incoming message sender matches configured owner."""
        if not self.chat_id:
            return False
        return str(from_chat_id).strip() == str(self.chat_id).strip()

    # -------------------------------------------------------------------------
    # Message Formatters
    # -------------------------------------------------------------------------

    def format_status_message(self):
        """Format current real-time telemetry snapshot."""
        if not self.state_ref:
            return "❌ Состояние телеметрии недоступно."

        st = self.state_ref.snapshot() if hasattr(self.state_ref, "snapshot") else dict(self.state_ref)
        pwr = st.get("power", 0.0)
        volt = st.get("voltage", 0.0)
        curr = st.get("current", 0.0)
        mode = st.get("current_mode", "idle")
        is_running = st.get("is_running", False)

        mode_title = st.get("mode_title", "⚪ ПОЛНЫЙ ПОКОЙ (ОТДЫХ)")
        dur_sec = st.get("cycle_duration_sec", 0) if is_running else st.get("rest_duration_sec", 0)
        dur_min = int(dur_sec / 60.0)

        # Mode icon
        if mode == "defrost":
            icon = "🔥"
        elif is_running:
            icon = "🟢"
        else:
            icon = "⚪"

        # Estimated temperatures
        temp_freezer = st.get("temp_freezer_model", "—")
        temp_fridge = st.get("temp_fridge_model", "—")

        # Quota & calibration
        reconcile = st.get("last_energy_reconciliation") or {}
        acc_str = f"{reconcile.get('accuracy_pct', 99.9)}%" if reconcile else "—"

        lines = [
            f"<b>🧊 Samsung RT34MB — Телеметрия</b>",
            "",
            f"<b>Режим:</b> {icon} {mode_title}",
            f"<b>Длительность:</b> {dur_min} мин ({dur_sec} сек)",
            "",
            f"⚡ <b>Мощность:</b> <code>{pwr:.1f} Вт</code>",
            f"🔌 <b>Напряжение:</b> <code>{volt:.1f} В</code>",
            f"📊 <b>Ток:</b> <code>{curr:.2f} А</code>",
            "",
            f"❄️ <b>Морозилка (модель):</b> <code>{temp_freezer}°C</code>",
            f"🥛 <b>Холодильное отделение:</b> <code>{temp_fridge}°C</code>",
            f"⚖️ <b>Точность учёта:</b> <code>{acc_str}</code>",
            "",
            f"<i>Обновлено: {datetime.now().strftime('%H:%M:%S (%d.%m.%Y)')}</i>"
        ]
        return "\n".join(lines)

    def format_today_message(self):
        """Format daily summary for today."""
        if not self.db_connect_fn:
            return "❌ База данных недоступна."

        today_str = datetime.now().strftime("%Y-%m-%d")
        tariff = 5.0
        currency = "₽"
        if self.config_fn:
            cfg = self.config_fn()
            tariff = float(cfg.get("electricity_tariff", 5.0))
            currency = str(cfg.get("currency", "₽"))

        try:
            conn = self.db_connect_fn()
            cur = conn.cursor()
            cur.execute("""
                SELECT 
                    count(*),
                    sum(case when cycle_type = 'cooling' then 1 else 0 end),
                    sum(case when cycle_type = 'defrost' then 1 else 0 end),
                    sum(case when cycle_type = 'cooling' then duration_sec else 0 end),
                    sum(case when cycle_type = 'cooling' then (avg_power * duration_sec / 3600.0 / 1000.0) else 0 end),
                    sum(case when cycle_type = 'defrost' then (avg_power * duration_sec / 3600.0 / 1000.0) else 0 end),
                    round(avg(avg_voltage), 1),
                    round(min(avg_voltage), 1)
                FROM cycles
                WHERE date(start_time) = ? AND duration_sec >= 120
            """, (today_str,))
            row = cur.fetchone()
            conn.close()

            cool_cycles = row[1] or 0
            defrost_cycles = row[2] or 0
            cool_sec = row[3] or 0
            cool_kwh = row[4] or 0.0
            defrost_kwh = row[5] or 0.0
            tot_kwh = cool_kwh + defrost_kwh
            avg_v = row[6] or 220.0
            min_v = row[7] or 220.0
            cost = tot_kwh * tariff

            cool_hours = round(cool_sec / 3600.0, 1)

            lines = [
                f"<b>⚡ Сводка за сегодня ({datetime.now().strftime('%d.%m.%Y')}):</b>",
                "",
                f"🔋 <b>Расход:</b> <code>{tot_kwh:.3f} кВт⋅ч</code> ({cost:.2f} {currency})",
                f"  • Компрессор: <code>{cool_kwh:.3f} кВт⋅ч</code>",
                f"  • ТЭН оттайки: <code>{defrost_kwh:.3f} кВт⋅ч</code>",
                "",
                f"🌀 <b>Циклов охлаждения:</b> {cool_cycles} ({cool_hours} ч работы)",
                f"🔥 <b>Циклов оттайки:</b> {defrost_cycles}",
                f"🔌 <b>Сеть:</b> среднее <code>{avg_v} В</code>, минимум <code>{min_v} В</code>",
            ]
            return "\n".join(lines)
        except Exception as e:
            return f"⚠️ Ошибка расчёта за сегодня: {e}"

    def format_weekly_digest(self):
        """Format comprehensive 7-day executive digest."""
        if not self.db_connect_fn:
            return "❌ База данных недоступна."

        tariff = 5.0
        currency = "₽"
        if self.config_fn:
            cfg = self.config_fn()
            tariff = float(cfg.get("electricity_tariff", 5.0))
            currency = str(cfg.get("currency", "₽"))

        try:
            conn = self.db_connect_fn()
            cur = conn.cursor()

            # 7-day cycles aggregation
            cur.execute("""
                SELECT 
                    count(*),
                    sum(case when cycle_type = 'cooling' then 1 else 0 end),
                    sum(case when cycle_type = 'defrost' then 1 else 0 end),
                    sum(case when cycle_type = 'cooling' then duration_sec else 0 end),
                    sum(case when cycle_type = 'cooling' then (avg_power * duration_sec / 3600.0 / 1000.0) else 0 end),
                    sum(case when cycle_type = 'defrost' then (avg_power * duration_sec / 3600.0 / 1000.0) else 0 end),
                    round(avg(avg_voltage), 1),
                    round(min(avg_voltage), 1),
                    round(avg(case when cycle_type = 'cooling' then duration_sec else null end) / 60.0, 1)
                FROM cycles
                WHERE start_time >= datetime('now', '-7 days') AND duration_sec >= 120
            """)
            r = cur.fetchone()

            cool_cycles = r[1] or 0
            df_cycles = r[2] or 0
            cool_sec = r[3] or 0
            cool_kwh = r[4] or 0.0
            df_kwh = r[5] or 0.0
            tot_kwh = cool_kwh + df_kwh
            avg_v = r[6] or 220.0
            min_v = r[7] or 220.0
            avg_cool_min = r[8] or 0.0

            # Total duty cycle KRV over 7 days
            total_period_sec = 7 * 86400.0
            krv = round(cool_sec / total_period_sec, 2)
            krv_badge = "🟢 Отличный" if krv <= 0.42 else ("🟡 Умеренный" if krv <= 0.50 else "🔴 Повышенный")

            # Voltage drops < 190V
            cur.execute("""
                SELECT count(*), round(sum(duration_sec)/3600.0, 1)
                FROM cycles
                WHERE start_time >= datetime('now', '-7 days') AND avg_voltage < 190 AND cycle_type = 'cooling'
            """)
            v_drop = cur.fetchone()
            v_drop_cycles = v_drop[0] or 0
            v_drop_hours = v_drop[1] or 0.0

            # Blackouts count
            cur.execute("""
                SELECT count(*), round(sum(duration_sec)/3600.0, 1)
                FROM blackouts
                WHERE start_time >= datetime('now', '-7 days')
            """)
            bo = cur.fetchone()
            bo_count = bo[0] or 0
            bo_hours = bo[1] or 0.0

            # Latest reconciliation
            cur.execute("""
                SELECT accuracy_pct, delta_kwh, date_str
                FROM energy_reconciliations
                ORDER BY id DESC LIMIT 1
            """)
            rec = cur.fetchone()
            conn.close()

            cost = tot_kwh * tariff

            lines = [
                "<b>📊 ЕЖЕНЕДЕЛЬНЫЙ ДАЙДЖЕСТ ЗДОРОВЬЯ ХОЛОДИЛЬНИКА</b>",
                f"<i>Период: последние 7 дней (Samsung RT34MB)</i>",
                "",
                f"💰 <b>Энергия и затраты:</b>",
                f"  • Всего: <b>{tot_kwh:.2f} кВт⋅ч</b> (<b>{cost:.2f} {currency}</b>)",
                f"  • Компрессор: <code>{cool_kwh:.2f} кВт⋅ч</code>",
                f"  • ТЭН оттайки No-Frost: <code>{df_kwh:.2f} кВт⋅ч</code>",
                "",
                f"🧊 <b>Диагностика компрессора:</b>",
                f"  • КРВ (Duty Cycle): <b>{krv}</b> ({krv_badge})",
                f"  • Всего циклов охлаждения: <b>{cool_cycles}</b>",
                f"  • Средняя длительность цикла: <b>{avg_cool_min} мин</b>",
                f"  • Циклов оттайки No-Frost: <b>{df_cycles}</b>",
                "",
                f"🔌 <b>Аудит электросети:</b>",
                f"  • Среднее напряжение: <code>{avg_v} В</code> (мин: <code>{min_v} В</code>)",
                f"  • Просадки &lt;190 В: <b>{v_drop_cycles} циклов</b> ({v_drop_hours} ч под риском)",
                f"  • Отключений света 220В: <b>{bo_count}</b> ({bo_hours} ч суммарно)",
                ""
            ]

            if rec:
                lines.append(f"⚖️ <b>Сверка с облаком Tuya:</b> точность <b>{rec[0]}%</b> (дельта {rec[1]} кВт⋅ч)")
            else:
                lines.append("⚖️ <b>Сверка с облаком Tuya:</b> активна каждый час")

            # General verdict
            if krv <= 0.45 and v_drop_cycles < 10:
                verdict = "🟢 <b>Вердикт:</b> Холодильник работает штатно и энергоэффективно. Уплотнители держат холод отлично."
            elif v_drop_cycles >= 10:
                verdict = "🟡 <b>Вердикт:</b> Компрессор исправен, но частые просадки напряжения сети снижают КПД. Рекомендуется стабилизатор."
            else:
                verdict = "🟡 <b>Вердикт:</b> КРВ слегка повышен. Проверьте чистоту радиатора конденсатора и зазоры от стены."
            lines.append("")
            lines.append(verdict)

            return "\n".join(lines)
        except Exception as e:
            logger.error("Error formatting weekly digest: %s", e)
            return f"⚠️ Ошибка формирования еженедельного дайджеста: {e}"

    def format_audit_message(self):
        """Format thermodynamic expert opinion."""
        if not self.state_ref:
            return "❌ Состояние телеметрии недоступно."

        st = self.state_ref.snapshot() if hasattr(self.state_ref, "snapshot") else dict(self.state_ref)
        volt = st.get("voltage", 220.0)
        mode = st.get("current_mode", "idle")

        lines = [
            "<b>👨‍🔧 ЭКСПЕРТНЫЙ АУДИТ ТЕХНИЧЕСКОГО СОСТОЯНИЯ</b>",
            "",
            "<b>Агрегат:</b> Samsung RT34MBTS (CoolTech Dynamic No-Frost)",
            "<b>Компрессор:</b> Samsung SK170K-T1U (R-134a, 160 г)",
            "",
            "<b>1. Теплофизический контур:</b>",
            "  • Двухкамерная компоновка с верхним морозильником удерживает холод до 8–10 ч при блэкаутах.",
            "  • Включение оттайки (~155 Вт) каждые 9–12 ч подтверждает нормальную циркуляцию испарителя.",
            "",
            "<b>2. Электрическая безопасность:</b>",
            f"  • Текущее напряжение сети: <code>{volt:.1f} В</code>",
            "  • Защитная пауза пуска 5 мин гарантирует выравнивание давлений хладагента перед повторным стартом.",
            "",
            "<b>3. Рекомендации по эксплуатации:</b>",
            "  • Сохраняйте зазор не менее 5 см от задней стенки для естественного охлаждения конденсатора.",
            "  • При падении напряжения ниже 185 В компрессор издаёт повышенный гул — защита автоматически зафиксирует событие.",
            "",
            f"<i>Текущий статус агрегата: {mode.upper()}</i>"
        ]
        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # Incoming Command Dispatcher
    # -------------------------------------------------------------------------

    def handle_command(self, text, from_chat_id):
        """Process incoming message or button tap."""
        if not self.is_authorized(from_chat_id):
            logger.warning("Rejected unauthorized message from chat_id: %s", from_chat_id)
            self.send_message(
                f"⛔ <b>Доступ ограничен.</b>\nВаш Chat ID: <code>{from_chat_id}</code> не авторизован в настройках монитора.",
                chat_id=from_chat_id
            )
            return

        cmd = text.strip().lower()

        if cmd in ("/start", "/help", "❓ помощь", "помощь"):
            msg = [
                "👋 <b>Добро пожаловать в бот мониторинга Samsung RT34MB!</b>",
                "",
                "Бот транслирует телеметрию с ТВ-бокса H96, следит за авариями электросети и формирует отчёты.",
                "",
                "<b>Быстрые кнопки:</b>",
                "• <b>🟢 Статус</b> — моментальный снимок мощности, напряжения и режима",
                "• <b>📊 Отчёт за неделю</b> — сводка расхода кВт⋅ч, затрат и здоровья",
                "• <b>⚡ За сегодня</b> — статистика работы компрессора за текущие сутки",
                "• <b>⚖️ Сверить счётчик</b> — калибровка с чипом розетки через Tuya Cloud",
                "• <b>🧠 Аудит</b> — заключение о состоянии агрегата",
                "",
                "<i>Кнопки управления закреплены внизу экрана ⬇️</i>"
            ]
            self.send_message("\n".join(msg), reply_markup=DEFAULT_REPLY_KEYBOARD)

        elif cmd in ("/status", "🟢 статус", "статус"):
            self.send_message(self.format_status_message(), reply_markup=DEFAULT_REPLY_KEYBOARD)

        elif cmd in ("/today", "⚡ за сегодня", "за сегодня"):
            self.send_message(self.format_today_message(), reply_markup=DEFAULT_REPLY_KEYBOARD)

        elif cmd in ("/week", "📊 отчёт за неделю", "отчёт за неделю", "отчет за неделю"):
            self.send_message(self.format_weekly_digest(), reply_markup=DEFAULT_REPLY_KEYBOARD)

        elif cmd in ("/audit", "🧠 аудит", "аудит"):
            self.send_message(self.format_audit_message(), reply_markup=DEFAULT_REPLY_KEYBOARD)

        elif cmd in ("/reconcile", "⚖️ сверить счётчик", "сверить счётчик", "сверить"):
            self.send_message("⏳ <i>Запуск облачной сверки с чипом розетки Tuya Cloud...</i>")
            if self.reconcile_fn:
                try:
                    res = self.reconcile_fn(force=True)
                    acc = res.get("accuracy_pct", 0)
                    loc = res.get("local_kwh", 0)
                    cld = res.get("cloud_kwh", 0)
                    dlt = res.get("delta_kwh", 0)
                    rep = res.get("reports_count", 0)
                    msg = [
                        "<b>⚖️ Результат сверки счётчика энергии:</b>",
                        f"• Точность совпадения: <b>{acc}%</b>",
                        f"• Локальный расчёт SQLite: <code>{loc:.3f} кВт⋅ч</code>",
                        f"• Аппаратный чип розетки: <code>{cld:.3f} кВт⋅ч</code>",
                        f"• Дельта (расхождение): <code>{dlt:.3f} кВт⋅ч</code>",
                        f"• Обработано отчётов розетки: <code>{rep}</code>",
                        f"• Статус: <b>{res.get('status', 'aligned')}</b>"
                    ]
                    self.send_message("\n".join(msg), reply_markup=DEFAULT_REPLY_KEYBOARD)
                except Exception as e:
                    self.send_message(f"❌ Ошибка сверки: {e}", reply_markup=DEFAULT_REPLY_KEYBOARD)
            else:
                self.send_message("❌ Модуль сверки недоступен.", reply_markup=DEFAULT_REPLY_KEYBOARD)

        else:
            self.send_message(
                "❓ Неизвестная команда. Воспользуйтесь кнопками меню внизу или командой /help.",
                reply_markup=DEFAULT_REPLY_KEYBOARD
            )

    # -------------------------------------------------------------------------
    # Watchdog & Scheduler Logic
    # -------------------------------------------------------------------------

    def check_watchdogs(self):
        """Check telemetry thresholds and fire emergency alerts with cooldowns."""
        if not self.enabled or not self.state_ref:
            return

        now = time.time()
        st = self.state_ref.snapshot() if hasattr(self.state_ref, "snapshot") else dict(self.state_ref)
        is_running = st.get("is_running", False)
        mode = st.get("current_mode", "idle")
        volt = st.get("voltage", 220.0)
        dur_sec = st.get("cycle_duration_sec", 0)

        # 1. Critical under-voltage alert (< 185V for > 90s)
        if is_running and volt > 50.0 and volt < 185.0:
            if self._low_v_start_time is None:
                self._low_v_start_time = now
            elif (now - self._low_v_start_time) >= 90.0 and (now - self._last_low_v_alert) > 1800.0:
                self._last_low_v_alert = now
                self.send_alert(
                    f"⚠️ <b>ВНИМАНИЕ: ОПАСНО НИЗКОЕ НАПРЯЖЕНИЕ!</b>\n\n"
                    f"Напряжение в сети: <code>{volt:.1f} В</code> (&lt;185 В).\n"
                    f"Компрессор работает под повышенной электрической нагрузкой уже >90 сек.\n"
                    f"Рекомендуется проверить вводной автомат или стабилизатор."
                )
        else:
            self._low_v_start_time = None

        # 2. Extended cooling cycle alert (> 60 minutes continuous)
        if is_running and mode == "cooling" and dur_sec >= 3600:
            if (now - self._last_long_cycle_alert) > 3600.0:
                self._last_long_cycle_alert = now
                dur_min = int(dur_sec / 60.0)
                self.send_alert(
                    f"🚨 <b>ДЛИТЕЛЬНЫЙ ЦИКЛ ОХЛАЖДЕНИЯ ({dur_min} мин)</b>\n\n"
                    f"Компрессор работает без остановки более 1 часа.\n"
                    f"Возможные причины:\n"
                    f"• Неплотно закрыта дверь холодильника или износ уплотнителя\n"
                    f"• Загружено большое количество теплых продуктов\n"
                    f"• Засор теплообменника конденсатора пылью"
                )

        # 3. Extended defrost alert (> 35 minutes continuous)
        if is_running and mode == "defrost" and dur_sec >= 2100:
            if (now - self._last_long_defrost_alert) > 3600.0:
                self._last_long_defrost_alert = now
                dur_min = int(dur_sec / 60.0)
                self.send_alert(
                    f"🔥 <b>АНОМАЛИЯ ОТТАЙКИ NO-FROST ({dur_min} мин)</b>\n\n"
                    f"ТЭН оттайки испарителя работает дольше 35 минут (норма 20–28 мин).\n"
                    f"Проверьте термопредохранитель и датчик окончания оттайки."
                )

        # 4. Weekly Digest Auto-Scheduler (Sunday 20:00)
        dt_now = datetime.now()
        today_date = dt_now.strftime("%Y-%m-%d")
        if dt_now.weekday() == 6 and dt_now.hour == 20 and dt_now.minute >= 0 and dt_now.minute <= 10:
            if self._last_weekly_sent_date != today_date:
                self._last_weekly_sent_date = today_date
                digest = self.format_weekly_digest()
                self.send_message(digest, reply_markup=DEFAULT_REPLY_KEYBOARD)

    def notify_blackout_resolved(self, start_iso, end_iso, dur_sec, safety_status):
        """Immediately alert user when 220V power outage has concluded."""
        if not self.enabled:
            return

        dur_min = int(dur_sec / 60.0)
        hours = dur_min // 60
        mins = dur_min % 60
        dur_str = f"{hours} ч {mins} мин" if hours > 0 else f"{mins} мин"

        msg = [
            "⚡ <b>ПИТАНИЕ 220В ВОССТАНОВЛЕНО!</b>",
            "",
            f"• Начало сбоя: <code>{start_iso[11:16]}</code>",
            f"• Конец сбоя: <code>{end_iso[11:16]}</code>",
            f"• Длительность блэкаута: <b>{dur_str}</b> ({dur_sec} сек)",
            f"• Оценка заморозки: <b>{safety_status}</b>",
            "",
            "<i>Автоматика выдержала паузу пуска и возобновила штатный мониторинг.</i>"
        ]
        self.send_alert("\n".join(msg))

    # -------------------------------------------------------------------------
    # Long Polling Loop
    # -------------------------------------------------------------------------

    def _poll_updates(self):
        """Long polling loop fetching incoming Telegram updates."""
        while not self._stop_event.is_set():
            if not self.enabled:
                time.sleep(3)
                continue

            try:
                payload = {
                    "offset": self._last_update_id + 1,
                    "timeout": 20,
                    "allowed_updates": ["message"]
                }
                res = self._api_call("getUpdates", payload, timeout=30)
                if res and res.get("ok"):
                    for update in res.get("result", []):
                        self._last_update_id = update["update_id"]
                        msg = update.get("message")
                        if not msg:
                            continue
                        text = msg.get("text")
                        chat = msg.get("chat", {})
                        chat_id = chat.get("id")
                        if text and chat_id:
                            self.handle_command(text, chat_id)
                else:
                    time.sleep(2)
            except Exception as e:
                logger.debug("Polling iteration error: %s", e)
                time.sleep(5)

    def start(self):
        """Start the background bot thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_updates, name="TelegramBotPoll", daemon=True)
        self._thread.start()
        logger.info("Telegram Bot started (enabled=%s)", self.enabled)

    def stop(self):
        """Signal bot thread to stop."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Telegram Bot stopped")
