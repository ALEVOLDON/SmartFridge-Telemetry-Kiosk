# 🔌 REST API Reference

Documentation for the **SmartFridge-Telemetry-Kiosk** embedded HTTP backend (`auto_monitor.py`).

The server runs on port `8088` (default: `http://0.0.0.0:8088`).

---

## 🛡️ Security & Access Control (LAN-Guard)

All `/api/*` endpoints are guarded by `@app.before_request`:
- **Allowed:** Loopback (`127.0.0.1`, `::1`), RFC1918 private subnets (`192.168.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`), Carrier-Grade NAT (`100.64.0.0/10` for Tailscale/VPN), and link-local.
- **Forbidden (HTTP 403):** Public WAN IP addresses are blocked with `{"error": "Forbidden: LAN-only service"}`.
- **CORS:** Automatic `Access-Control-Allow-Origin: *` headers for seamless integration with browser dashboards and iPad kiosks.
- **Cache-Control:** `no-cache, no-store, must-revalidate, max-age=0` ensures real-time telemetry freshness.

---

## 📡 API Endpoints

### 1. `GET /api/status`
Returns the complete real-time telemetry snapshot, compressor operational state, chamber temperature estimates, and system health.

**Response Example (HTTP 200):**
```json
{
  "app_version": "PRO v3.11.0",
  "app_version_name": "Cyberpunk Boot Splash Screen & UI Experience",
  "app_release_date": "26.09.2026",
  "mode": "rest",
  "power": 0.0,
  "voltage": 218.4,
  "current": 0.0,
  "temp_freezer": -18.2,
  "temp_fridge": 4.1,
  "timer_sec": 1420,
  "timer_formatted": "00:23:40",
  "avg_duty_cycle": 0.39,
  "blackout_active": false,
  "blackout_duration_sec": 0,
  "blackout_duration_str": "",
  "cloud_quota": {
    "days_left": 999,
    "forecast_text": "100% экономия: прямые Wi-Fi сокеты"
  },
  "last_blackout": {
    "date": "06.09.2026",
    "start": "14:10",
    "end": "15:45",
    "duration_str": "1 ч 35 мин",
    "food_safety": "Заморозка полностью безопасна"
  }
}
```

---

### 2. `GET /api/history`
Returns structured history of completed compressor cooling cycles, defrost cycles, and power outages from SQLite.

**Query Parameters:**
- `limit` *(int, default: 30)*: Number of recent cycles to retrieve.
- `offset` *(int, default: 0)*: Pagination offset for full archive viewing.
- `all` *(bool, default: false)*: When `true`, returns all records in compact form.

**Response Example (HTTP 200):**
```json
{
  "cycles": [
    {
      "id": 152,
      "date": "26.09.2026",
      "start": "14:15:02",
      "end": "14:46:18",
      "duration_sec": 1876,
      "duration_formatted": "31 мин 16 сек",
      "rest_duration_sec": 2820,
      "rest_formatted": "47 мин 00 сек",
      "avg_power": 136.4,
      "avg_voltage": 216.5,
      "duty_cycle": 0.399,
      "mode": "cooling",
      "verdict": "Идеальная норма (КРВ 0.40)"
    }
  ],
  "total_count": 152,
  "summary": {
    "overall_krv": "0.39",
    "krv_status": "Эталонный баланс",
    "avg_cooling_min": "31.8",
    "avg_rest_min": "47.2"
  }
}
```

---

### 3. `GET /api/analytics`
Server-cached deep analytics engine calculating energy consumption, No-Frost defrost health, and mains voltage quality.

**Response Example (HTTP 200):**
```json
{
  "energy": {
    "daily_kwh": 1.15,
    "monthly_kwh": 34.5,
    "daily_cost": 5.75,
    "monthly_cost": 172.5,
    "tariff": 5.0,
    "currency": "₽",
    "defrost_share_pct": 14.2,
    "motor_share_pct": 85.8
  },
  "health_score": 88,
  "defrost_health": {
    "status": "normal",
    "average_duration_min": 24.5,
    "heater_power_avg": 158.2,
    "total_defrost_count": 12
  },
  "voltage_health": {
    "average_volts": 214.2,
    "low_voltage_hours": 0.4,
    "normal_voltage_pct": 98.2
  }
}
```

---

### 4. `GET /api/changelog`
Returns structured release history and version notes directly from `changelog.json`.

**Response Example (HTTP 200):**
```json
{
  "status": "ok",
  "version": "3.11.0",
  "version_string": "PRO v3.11.0",
  "release_title": "Cyberpunk Boot Splash Screen & UI Experience",
  "release_date": "26.09.2026",
  "changelog": [
    {
      "version": "3.11.0",
      "title": "Cyberpunk Boot Splash Screen & UI Experience",
      "date": "26.09.2026",
      "points": [
        {
          "lead": "Интерактивная неоновая заставка (Cyber Splash Screen)",
          "desc": "футуристическая анимация орбитальных колец, круговой радар-сканер и светящаяся эмблема холодильника..."
        }
      ]
    }
  ]
}
```

---

### 5. `GET /api/tariff` & `POST /api/tariff`
Gets or updates the electricity tariff rate and currency symbol.

**POST Body Example:**
```json
{
  "tariff": 5.50,
  "currency": "₽"
}
```

---

### 6. `POST /api/reconcile-energy`
Triggers immediate hardware energy reconciliation comparing local Simpson/Riemann integration against the smart plug's hardware energy counter chip (`add_ele` register via Tuya Cloud).

**POST Body (Optional):**
```json
{
  "date": "2026-09-26"
}
```

---

### 7. `POST /api/test_telegram`
Sends a test diagnostic ping to the configured Telegram Bot chat.

**POST Body:**
```json
{
  "telegram_bot_token": "OPTIONAL_OVERRIDE_TOKEN",
  "telegram_chat_id": "OPTIONAL_OVERRIDE_CHAT_ID"
}
```
