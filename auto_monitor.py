import os
import sys
import time
import json
import copy
import socket
import sqlite3
import argparse
import threading
import signal
import subprocess
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
import tinytuya
from fridge_logic import (
    DEFROST_MAX_SEC,
    DEFROST_MIN_SEC,
    DEFROST_POWER_MAX,
    DEFROST_POWER_MIN,
    MIN_COOLING_BEFORE_DEFROST_SEC,
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

try:
    from winotify import Notification, audio
    HAS_WINOTIFY = True
except ImportError:
    HAS_WINOTIFY = False

try:
    from telegram_bot import FridgeTelegramBot, DEFAULT_REPLY_KEYBOARD
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False
    FridgeTelegramBot = None
    DEFAULT_REPLY_KEYBOARD = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
DB_FILE = os.path.join(APP_DIR, "fridge_data.db")
STATIC_DIR = os.path.join(APP_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR)

class LockedState:
    """Thread-safe telemetry bag: poller writes, Flask reads a deep snapshot."""

    def __init__(self, data):
        self._data = dict(data)
        self._lock = threading.RLock()

    def __getitem__(self, key):
        with self._lock:
            return self._data[key]

    def __setitem__(self, key, value):
        with self._lock:
            self._data[key] = value

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._data)

# Global Telemetry State with Temperature and Blackout Tracking
state = LockedState({
    "connected": False,
    "power": 0.0,
    "voltage": 0.0,
    "current": 0.0,
    "is_running": False,
    "current_mode": "idle",
    "mode_title": "⚪ ПОЛНЫЙ ПОКОЙ (ОТДЫХ)",
    "cycle_start_time": None,
    "cycle_duration_sec": 0,
    "rest_start_time": None,
    "rest_duration_sec": 0,
    "last_update": None,
    "error_message": "",
    "protection_active": False,
    "protection_lockout_sec": 0,
    "protection_status_text": "Пауза пуска снята",
    "notifications_enabled": True,
    "avg_duty_cycle": 0.38,
    "temp_freezer": -18.2,
    "temp_fridge": 4.1,
    "temp_sensor_connected": False,
    "temp_sensor_id": "",
    "temp_is_estimated": True,
    "temp_freezer_estimated": True,
    "temp_fridge_estimated": True,
    "connection_source": "",
    "last_blackout": {
        "detected": False,
        "start": "—",
        "end": "—",
        "duration_str": "Отключений не зафиксировано",
        "duration_sec": 0,
        "date": "—",
        "food_safety": "🟢 Электросеть стабильна"
    },
    "last_energy_reconciliation": None
})

_poller_thread = None
_telegram_bot = None
_schema_ready = False
_schema_lock = threading.Lock()

def is_localhost_request():
    """Checks if request originated from the server host itself."""
    return is_loopback_ip(request.remote_addr)

def is_lan_request():
    """Loopback or RFC1918 / link-local / CGNAT (Tailscale)."""
    return is_private_ip(request.remote_addr)

def send_pc_toast(title, message, is_warning=False):
    """Sends native Windows Desktop notification in bottom-right corner"""
    if not state.get("notifications_enabled", True) or not HAS_WINOTIFY:
        return
    try:
        toast = Notification(
            app_id="SmartFridge Monitor",
            title=title,
            msg=message,
            duration="short"
        )
        if is_warning:
            toast.set_audio(audio.Reminder, loop=False)
        else:
            toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception as e:
        print("Toast error:", e)

def db_connect():
    """SQLite connection with WAL and busy timeout so poller and Flask don't lock up."""
    global _schema_ready
    conn = sqlite3.connect(DB_FILE, timeout=10.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        pass
    if not _schema_ready:
        with _schema_lock:
            if not _schema_ready:
                _ensure_schema(conn)
                conn.commit()
                _schema_ready = True
    return conn

def _ensure_schema(conn):
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            power REAL,
            voltage REAL,
            current REAL,
            is_running INTEGER,
            mode TEXT,
            temp_freezer REAL,
            temp_fridge REAL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT,
            end_time TEXT,
            duration_sec INTEGER,
            avg_power REAL,
            avg_voltage REAL,
            cycle_type TEXT,
            UNIQUE(start_time, end_time)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS blackouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT,
            end_time TEXT,
            duration_sec INTEGER,
            food_safety_status TEXT,
            UNIQUE(start_time, end_time)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS cloud_usage (
            day_date TEXT PRIMARY KEY,
            calls_count INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS energy_reconciliations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_str TEXT UNIQUE,
            local_kwh REAL,
            cloud_kwh REAL,
            delta_kwh REAL,
            accuracy_pct REAL,
            cloud_reports_count INTEGER,
            reconciled_at TEXT,
            status TEXT
        )
    ''')
    for col in ["mode TEXT", "temp_freezer REAL", "temp_fridge REAL"]:
        try:
            c.execute(f"ALTER TABLE measurements ADD COLUMN {col}")
        except Exception:
            pass
    try:
        c.execute("ALTER TABLE cycles ADD COLUMN cycle_type TEXT")
    except Exception:
        pass
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cycles_start_end ON cycles(start_time, end_time)")
    except Exception:
        pass

def init_db():
    conn = db_connect()
    conn.close()

def record_cloud_call(n=1):
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        conn = db_connect()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO cloud_usage (day_date, calls_count)
            VALUES (?, ?)
            ON CONFLICT(day_date) DO UPDATE SET calls_count = calls_count + ?
        ''', (today, n, n))
        conn.commit()
        conn.close()
    except Exception:
        pass

def get_cloud_quota_stats():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        month_start = datetime.now().strftime("%Y-%m-01")
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT calls_count FROM cloud_usage WHERE day_date = ?", (today,))
        row_today = cur.fetchone()
        today_calls = int(row_today[0]) if (row_today and row_today[0] is not None) else 0
        
        cur.execute("SELECT SUM(calls_count) FROM cloud_usage WHERE day_date >= ?", (month_start,))
        row_month = cur.fetchone()
        month_calls = int(row_month[0]) if (row_month and row_month[0] is not None) else 0
        conn.close()
        
        total_limit = 50000
        rem = max(0, total_limit - month_calls)
        is_local = (state.get("connection_source") == "local_wifi")
        
        if is_local:
            forecast = "100% экономия: 0 запросов/день при LAN (хватит навсегда)"
            days_left = 999
        else:
            rate = max(today_calls, 1400)
            days_left = max(1, rem // rate)
            forecast = f"Эко-режим: хватит на ~{days_left} дн."
            
        return {
            "total_limit": total_limit,
            "used_month": month_calls,
            "used_today": today_calls,
            "remaining": rem,
            "forecast_text": forecast,
            "days_left": days_left,
            "is_safe": rem > 3000,
            "is_local": is_local
        }
    except Exception:
        return {
            "total_limit": 50000,
            "used_month": 0,
            "used_today": 0,
            "remaining": 50000,
            "forecast_text": "100% экономия (LAN)",
            "days_left": 999,
            "is_safe": True,
            "is_local": True
        }

def load_config():
    cfg = {
        "api_region": "eu",
        "api_key": "",
        "api_secret": "",
        "device_id": "",
        "temp_sensor_id": "",
        "local_key": "",
        "device_ip": "",
        "device_mac": "",
        "local_version": 3.5,
        "lan_subnet": "192.168.0.0/24",
        "last_stop_time": None,
        "current_start_time": None,
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "telegram_enabled": False
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception:
            pass
    return cfg

def save_config(cfg):
    tmp_file = CONFIG_FILE + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        os.replace(tmp_file, CONFIG_FILE)
    except Exception:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)

# No Frost timer counts compressor ON time (~8–10 h), then a 10–25 min
# sheath heater at ~150–175 W. SK170K cooling is 115–142 W steady.
COMPRESSOR_LOCKOUT_SEC = 180
LONG_RUN_WARN_SEC = 25200  # 7 h — service guide fault threshold
CLOUD_SYNC_EVERY_POLLS = 300  # ~20 min at 4 s interval
PRUNE_EVERY_POLLS = 720

_cloud_client = None
_cloud_client_cfg = None

def get_cloud_client(cfg):
    global _cloud_client, _cloud_client_cfg
    key = (cfg.get("api_region"), cfg.get("api_key"), cfg.get("api_secret"), cfg.get("device_id"))
    if _cloud_client is None or _cloud_client_cfg != key:
        _cloud_client = tinytuya.Cloud(
            apiRegion=cfg.get("api_region", "eu"),
            apiKey=cfg.get("api_key", "").strip(),
            apiSecret=cfg.get("api_secret", "").strip(),
            apiDeviceID=cfg.get("device_id", "").strip()
        )
        _cloud_client_cfg = key
    return _cloud_client

_last_reconcile_time = 0.0
_last_hourly_reconcile_hour = -1
_reconcile_lock = threading.Lock()

def get_latest_reconciliation():
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute('''
            SELECT date_str, local_kwh, cloud_kwh, delta_kwh, accuracy_pct, cloud_reports_count, reconciled_at, status
            FROM energy_reconciliations
            ORDER BY date_str DESC, id DESC LIMIT 1
        ''')
        row = cur.fetchone()
        conn.close()
        if row:
            return {
                "date": row[0],
                "local_kwh": row[1],
                "cloud_kwh": row[2],
                "delta_kwh": row[3],
                "accuracy_pct": row[4],
                "reports_count": row[5],
                "reconciled_at": row[6],
                "status": row[7]
            }
    except Exception:
        pass
    return None

def reconcile_energy_with_cloud(target_date_str=None, force=False):
    """
    Hourly/daily energy reconciliation between local SQLite cycles integration
    and the smart plug's hardware metering chip (add_ele DP log in Tuya Cloud).
    Consumes exactly 1-2 cloud API requests per reconciliation.
    """
    global _last_reconcile_time
    now = datetime.now()
    if not target_date_str:
        target_date_str = now.strftime("%Y-%m-%d")

    with _reconcile_lock:
        if not force and (time.time() - _last_reconcile_time) < 300:
            latest = state.get("last_energy_reconciliation") or get_latest_reconciliation()
            return {"success": True, "cached": True, "data": latest}
        _last_reconcile_time = time.time()

        conn = db_connect()
        cur = conn.cursor()

        # 1. Local SQLite integration for target date
        next_date_str = (datetime.strptime(target_date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        cur.execute(
            "SELECT SUM(duration_sec * avg_power / 3600000.0) FROM cycles WHERE start_time >= ? AND start_time < ?",
            (target_date_str, next_date_str)
        )
        row = cur.fetchone()
        local_kwh = float(row[0]) if (row and row[0] is not None) else 0.0

        # If reconciling today and compressor is currently running, add in-progress cycle energy
        if target_date_str == now.strftime("%Y-%m-%d") and state.get("is_running") and state.get("cycle_start_time"):
            try:
                st_dt = parse_iso(state["cycle_start_time"])
                if st_dt and st_dt.strftime("%Y-%m-%d") == target_date_str:
                    in_progress_sec = max(0, int((now - st_dt).total_seconds()))
                    cur_p = float(state.get("power") or 130.0)
                    local_kwh += (in_progress_sec * cur_p / 3600000.0)
            except Exception:
                pass

        # 2. Tuya Cloud hardware add_ele logs
        cfg = load_config()
        c = get_cloud_client(cfg)
        dev_id = (cfg.get("device_id") or "").strip()
        if not dev_id or not (cfg.get("api_key") or "").strip():
            conn.close()
            return {"success": False, "error": "Tuya Cloud credentials not configured"}

        start_dt = datetime.strptime(target_date_str, "%Y-%m-%d")
        end_dt = start_dt + timedelta(days=1)
        if end_dt > now:
            end_dt = now

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        try:
            logs_res = c.getdevicelog(dev_id, start=start_ms, end=end_ms, params={"codes": "add_ele"}, max_fetches=3)
            fetches = logs_res.get("fetches", 1) if isinstance(logs_res, dict) else 1
            record_cloud_call(fetches)
        except Exception as e:
            conn.close()
            return {"success": False, "error": f"Tuya Cloud request error: {e}"}

        entries = logs_res.get("result", {}).get("logs", []) if isinstance(logs_res, dict) else []
        cloud_wh = sum(int(e.get("value", 0)) for e in entries)
        cloud_kwh = round(cloud_wh / 1000.0, 3)
        local_kwh = round(local_kwh, 3)

        delta_kwh = round(abs(local_kwh - cloud_kwh), 3)
        base = max(cloud_kwh, local_kwh, 0.001)
        accuracy_pct = round(max(0.0, 100.0 - (delta_kwh / base * 100.0)), 1)

        now_iso = now.isoformat()
        status_str = "aligned" if accuracy_pct >= 90.0 else "divergent"
        if cloud_kwh == 0 and local_kwh == 0:
            accuracy_pct = 100.0
            status_str = "no_consumption"

        cur.execute('''
            INSERT INTO energy_reconciliations 
            (date_str, local_kwh, cloud_kwh, delta_kwh, accuracy_pct, cloud_reports_count, reconciled_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date_str) DO UPDATE SET
                local_kwh = excluded.local_kwh,
                cloud_kwh = excluded.cloud_kwh,
                delta_kwh = excluded.delta_kwh,
                accuracy_pct = excluded.accuracy_pct,
                cloud_reports_count = excluded.cloud_reports_count,
                reconciled_at = excluded.reconciled_at,
                status = excluded.status
        ''', (target_date_str, local_kwh, cloud_kwh, delta_kwh, accuracy_pct, len(entries), now_iso, status_str))
        conn.commit()
        conn.close()

        res = {
            "success": True,
            "date": target_date_str,
            "local_kwh": local_kwh,
            "cloud_kwh": cloud_kwh,
            "delta_kwh": delta_kwh,
            "accuracy_pct": accuracy_pct,
            "reports_count": len(entries),
            "reconciled_at": now_iso,
            "status": status_str
        }
        state["last_energy_reconciliation"] = res
        return res

def cooling_since_last_defrost_sec():
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT MAX(end_time) FROM cycles WHERE cycle_type = 'defrost'")
        last_d = (cur.fetchone() or [None])[0]
        if last_d:
            cur.execute(
                "SELECT COALESCE(SUM(duration_sec), 0) FROM cycles WHERE cycle_type = 'cooling' AND start_time >= ?",
                (last_d,),
            )
        else:
            cur.execute("SELECT COALESCE(SUM(duration_sec), 0) FROM cycles WHERE cycle_type = 'cooling'")
        total = int((cur.fetchone() or [0])[0] or 0)
        conn.close()
        return total
    except Exception:
        return 0

def reclassify_stored_cycles():
    """Walk cycles in time and rewrite cycle_type with the compressor-hour rule."""
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, start_time, end_time, duration_sec, avg_power, avg_voltage, cycle_type FROM cycles ORDER BY start_time ASC, id ASC"
        )
        rows = cur.fetchall()
        cooling_acc = 0
        for cid, st, et, dur, avg_p, avg_v, old_type in rows:
            dur = int(dur or 0)
            avg_p = float(avg_p or 0)
            powers = None
            if DEFROST_MIN_SEC <= dur <= DEFROST_MAX_SEC and cooling_acc >= MIN_COOLING_BEFORE_DEFROST_SEC and DEFROST_POWER_MIN <= avg_p <= DEFROST_POWER_MAX:
                try:
                    cur.execute("SELECT power FROM measurements WHERE timestamp >= ? AND timestamp <= ? AND power > 50.0", (st, et))
                    p_list = [r[0] for r in cur.fetchall()]
                    if len(p_list) >= 6:
                        powers = p_list
                except Exception:
                    pass
            new_type = classify_cycle(avg_p, dur, cooling_acc, powers=powers, voltage=avg_v)
            if new_type != old_type:
                cur.execute("UPDATE cycles SET cycle_type = ? WHERE id = ?", (new_type, cid))
            if new_type == "cooling":
                cooling_acc += int(dur or 0)
            else:
                cooling_acc = 0
        conn.commit()
        conn.close()
    except Exception:
        pass

def update_restart_lockout(is_active):
    if is_active:
        state["protection_active"] = False
        state["protection_lockout_sec"] = 0
        state["protection_status_text"] = "Компрессор в работе"
        return
    lockout = max(0, COMPRESSOR_LOCKOUT_SEC - int(state.get("rest_duration_sec") or 0))
    state["protection_lockout_sec"] = lockout
    state["protection_active"] = lockout > 0
    if lockout > 0:
        state["protection_status_text"] = f"Пауза пуска компрессора: {lockout} с"
    else:
        state["protection_status_text"] = "Пауза пуска снята"

def get_latest_end_time():
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT MAX(end_time) FROM cycles")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return None

def apply_idle_rest_clock():
    """Rest clock always follows the last saved cycle end, never a Tuya glitch."""
    db_last_end = get_latest_end_time()
    if db_last_end:
        state["rest_start_time"] = db_last_end
    if state.get("rest_start_time"):
        r_start_dt = parse_iso(state["rest_start_time"])
        if r_start_dt:
            state["rest_duration_sec"] = max(0, int((datetime.now() - r_start_dt).total_seconds()))
            return
    state["rest_duration_sec"] = 0

def update_last_blackout_state():
    global state
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT start_time, end_time, duration_sec, food_safety_status FROM blackouts ORDER BY start_time DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if row:
            st_iso, end_iso, dur_sec, _stored_safety = row
            try:
                dt_st = datetime.fromisoformat(st_iso)
                dt_end = datetime.fromisoformat(end_iso)
                dur_str = format_gap_str(dur_sec) or "—"
                
                state["last_blackout"] = {
                    "detected": True,
                    "start": dt_st.strftime("%H:%M"),
                    "end": dt_end.strftime("%H:%M"),
                    "date": dt_st.strftime("%d.%m.%Y"),
                    "duration_str": dur_str,
                    "duration_sec": dur_sec,
                    "food_safety": food_safety_label(dur_sec)
                }
            except Exception:
                pass
    except Exception:
        pass

def sync_cloud_history():
    """Fetches historical logs from Tuya Cloud ONLY if local database is empty"""
    cfg = load_config()
    if not (cfg.get("api_key") and cfg.get("device_id") and cfg.get("api_secret")):
        return False
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM cycles")
        c_count = cur.fetchone()[0]
        conn.close()
        if c_count > 5:
            return True
            
        c = get_cloud_client(cfg)
        now_ts = int(time.time() * 1000)
        start_ts = now_ts - (48 * 3600 * 1000)
        res = c.getdevicelog(
            deviceid=cfg["device_id"].strip(),
            start=start_ts,
            end=now_ts,
            evtype=7,
            size=0,
            max_fetches=5
        )
        logs = (res.get("result") or {}).get("logs") or []
        if not logs:
            return False
        
        events = []
        all_pings = []
        reboot_timestamps = set()
        for row in reversed(logs):
            t_sec = row['event_time'] / 1000.0
            all_pings.append(t_sec)
            code = row.get('code')
            if code in ['relay_status', 'electricity_coe', 'power_coe', 'light_mode']:
                reboot_timestamps.add(int(t_sec))
            if code == 'cur_power':
                raw_val = float(row.get('value', 0))
                val = raw_val / 10.0 if raw_val > 500 else raw_val
                dt_iso = datetime.fromtimestamp(t_sec).isoformat()
                events.append((t_sec, dt_iso, val))
        
        all_pings = sorted(list(set(all_pings)))
        
        conn = db_connect()
        cur = conn.cursor()
        
        # 1. Detect True Blackouts (offline gap >= 1 hour OR >= 30 min with confirmed hardware reboot)
        for i in range(1, len(all_pings)):
            t_prev = all_pings[i-1]
            t_curr = all_pings[i]
            gap_sec = int(t_curr - t_prev)
            
            is_hardware_reboot = any(abs(int(t_curr) - r_ts) <= 15 for r_ts in reboot_timestamps)
            
            if gap_sec >= 3600 or (gap_sec >= 1800 and is_hardware_reboot):
                dt_prev = datetime.fromtimestamp(t_prev).isoformat()
                dt_curr = datetime.fromtimestamp(t_curr).isoformat()
                safety = food_safety_label(gap_sec)
                
                cur.execute('''
                    INSERT OR IGNORE INTO blackouts (start_time, end_time, duration_sec, food_safety_status)
                    VALUES (?, ?, ?, ?)
                ''', (dt_prev, dt_curr, gap_sec, safety))
        
        # 2. Reconstruct Cycles (Blackout-Aware)
        cur.execute('SELECT start_time, end_time FROM blackouts')
        blackout_ranges = []
        for b_st, b_end in cur.fetchall():
            try:
                t1 = datetime.fromisoformat(b_st).timestamp()
                t2 = datetime.fromisoformat(b_end).timestamp()
                blackout_ranges.append((t1, t2))
            except Exception:
                pass

        def in_blackout_span(t_a, t_b):
            for b1, b2 in blackout_ranges:
                if (t_a <= b1 and t_b >= b2) or (b1 <= t_b <= b2) or (b1 <= t_a <= b2):
                    return True
            return False

        in_cycle = False
        c_start_ts = None
        c_start_iso = None
        powers = []
        cooling_acc = 0
        
        for i in range(len(events)):
            t_sec, dt_iso, val = events[i]
            
            # If a blackout occurred between previous event and current event, terminate pre-blackout cycle!
            if in_cycle and i > 0:
                t_prev, dt_prev, val_prev = events[i-1]
                if (t_sec - t_prev) >= 1800 or in_blackout_span(t_prev, t_sec):
                    dur = int(t_prev - c_start_ts)
                    if dur >= 180:
                        avg_p = sum(powers) / len(powers) if powers else 130.0
                        c_type = classify_cycle(avg_p, dur, cooling_acc, powers)
                        cur.execute('''
                            INSERT OR IGNORE INTO cycles (start_time, end_time, duration_sec, avg_power, avg_voltage, cycle_type)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (c_start_iso, dt_prev, dur, round(avg_p, 1), 226.0, c_type))
                        if c_type == "cooling":
                            cooling_acc += dur
                        else:
                            cooling_acc = 0
                    in_cycle = False
                    powers = []
                    c_start_ts = None
                    c_start_iso = None
            
            if val > 30.0:
                if not in_cycle:
                    in_cycle = True
                    c_start_ts = t_sec
                    c_start_iso = dt_iso
                    powers = [val]
                else:
                    powers.append(val)
            else:
                if in_cycle:
                    in_cycle = False
                    dur = int(t_sec - c_start_ts)
                    if dur >= 180:
                        avg_p = sum(powers) / len(powers) if powers else 130.0
                        c_type = classify_cycle(avg_p, dur, cooling_acc, powers)
                        cur.execute('''
                            INSERT OR IGNORE INTO cycles (start_time, end_time, duration_sec, avg_power, avg_voltage, cycle_type)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (c_start_iso, dt_iso, dur, round(avg_p, 1), 226.0, c_type))
                        if c_type == "cooling":
                            cooling_acc += dur
                        else:
                            cooling_acc = 0
                    powers = []
                    c_start_ts = None
                    c_start_iso = None
        
        conn.commit()
        conn.close()
        reclassify_stored_cycles()
        
        update_last_blackout_state()
        return True
    except Exception as e:
        print("Cloud sync notice:", e)
        return False

def check_cold_boot_blackout():
    """Detect if the TV Box just rebooted after a power cut (cold boot gap >= 900s)."""
    try:
        uptime_sec = 0.0
        try:
            with open("/proc/uptime", "r") as f:
                uptime_sec = float(f.read().split()[0])
        except Exception:
            return
        
        # Only evaluate on recent system reboot (< 15 minutes since OS booted)
        if uptime_sec > 900:
            return
            
        now = datetime.now()
        boot_time = now - timedelta(seconds=uptime_sec)
        
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT timestamp, power, voltage FROM measurements ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            conn.close()
            return
            
        last_ts_str = row[0]
        last_dt = parse_iso(last_ts_str)
        if not last_dt:
            conn.close()
            return
            
        gap_sec = (boot_time - last_dt).total_seconds()
        # If the server host itself was unpowered for >= 15 minutes:
        if gap_sec >= 900:
            cur.execute("SELECT COUNT(*) FROM blackouts WHERE start_time = ?", (last_ts_str,))
            if cur.fetchone()[0] == 0:
                safety = food_safety_label(gap_sec)
                end_iso = now.isoformat()
                cur.execute("""
                    INSERT INTO blackouts (start_time, end_time, duration_sec, food_safety_status)
                    VALUES (?, ?, ?, ?)
                """, (last_ts_str, end_iso, int(gap_sec), safety))
                
                # Close pre-blackout cycle if dangling
                cfg_c = load_config()
                c_start = cfg_c.get("current_start_time")
                if c_start and c_start < last_ts_str:
                    c_st_dt = parse_iso(c_start)
                    if c_st_dt:
                        c_dur = int((last_dt - c_st_dt).total_seconds())
                        if c_dur >= 60:
                            cur.execute("""
                                INSERT OR IGNORE INTO cycles (start_time, end_time, duration_sec, avg_power, avg_voltage, cycle_type)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (c_start, last_ts_str, c_dur, float(row[1] or 135.0), float(row[2] or 210.0), "cooling"))
                cfg_c["current_start_time"] = None
                save_config(cfg_c)
                conn.commit()
                update_last_blackout_state()
                if _telegram_bot:
                    try:
                        _telegram_bot.notify_blackout_resolved(last_ts_str, end_iso, int(gap_sec), safety)
                    except Exception:
                        pass
        conn.close()
    except Exception:
        pass

def find_open_cycle_start():
    """First sustained compressor-on run after the last saved cycle end or blackout.

    Ignores 1-sample Tuya glitches and idle rows written during a process restart.
    """
    last_end = get_latest_end_time() or "1970-01-01T00:00:00"
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT MAX(end_time) FROM blackouts WHERE end_time > ?", (last_end,))
        b_end = (cur.fetchone() or [None])[0]
        if b_end and b_end > last_end:
            last_end = b_end
        cur.execute(
            "SELECT timestamp, power FROM measurements WHERE timestamp > ? ORDER BY timestamp",
            (last_end,),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return None
    run_start = None
    run_n = 0
    last_closed_start = None
    for ts, p in rows:
        if (p or 0) > 35.0:
            if run_start is None:
                run_start = ts
                run_n = 1
            else:
                run_n += 1
        else:
            if run_n >= 3:
                last_closed_start = run_start
            run_start = None
            run_n = 0
    if run_n >= 3:
        return run_start
    return last_closed_start

def persist_open_cycle():
    """Keep the in-progress start time across SIGTERM restarts."""
    if not state.get("is_running") or not state.get("cycle_start_time"):
        return
    try:
        cfg_save = load_config()
        cfg_save["current_start_time"] = state["cycle_start_time"]
        save_config(cfg_save)
    except Exception:
        pass

def _on_process_stop(signum, frame):
    persist_open_cycle()
    if _telegram_bot:
        try:
            _telegram_bot.stop()
        except Exception:
            pass
    raise SystemExit(0)

_local_dev = None
_local_ident = None
_local_fail_until = 0.0
_last_ip_scan = 0.0
_last_lan_rule = 0.0

def _lan_cidr(cfg=None):
    cfg = cfg or load_config()
    return str(parse_lan_network(cfg.get("lan_subnet") or os.environ.get("FRIDGE_LAN_SUBNET")))

def _ensure_lan_route():
    """Keep the configured LAN subnet on eth0; byedpi tun0 otherwise swallows the plug."""
    global _last_lan_rule
    now = time.time()
    if now - _last_lan_rule < 30:
        return
    _last_lan_rule = now
    cidr = _lan_cidr()
    try:
        subprocess.run(["ip", "rule", "del", "pref", "9000"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        subprocess.run(["ip", "rule", "add", "to", cidr, "lookup", "main", "pref", "9000"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        subprocess.run(["ip", "rule", "del", "pref", "9001"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        subprocess.run(["ip", "rule", "add", "from", cidr, "lookup", "main", "pref", "9001"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
    except Exception:
        pass

def _tcp_open(ip, port=6668, timeout=0.5):
    try:
        sock = socket.create_connection((ip, int(port)), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False

def _norm_mac(mac):
    return (mac or "").strip().lower().replace(":", "-").replace(".", "-")

def _arp_pairs():
    pairs = []
    blobs = []
    try:
        with open("/proc/net/arp", "r", encoding="utf-8", errors="replace") as f:
            blobs.append(f.read())
    except Exception:
        pass
    for cmd in (["ip", "-o", "neigh"], ["ip", "neigh"], ["arp", "-a"]):
        try:
            blobs.append(subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=3).decode("utf-8", "replace"))
        except Exception:
            continue
    for blob in blobs:
        for raw in blob.replace(":", "-").splitlines():
            low = raw.lower()
            ip = None
            mac = None
            for tok in low.replace("(", " ").replace(")", " ").split():
                if tok.count(".") == 3 and tok[0].isdigit():
                    ip = tok.strip(",")
                elif tok.count("-") == 5 and len(tok) >= 17:
                    mac = tok[:17]
            if ip and mac and not mac.startswith("00-00-00"):
                pairs.append((ip, mac))
    return pairs

def _scan_tuya_port(cfg=None):
    try:
        from concurrent.futures import ThreadPoolExecutor
    except Exception:
        return []
    hosts = tuya_scan_hosts(parse_lan_network((cfg or load_config()).get("lan_subnet") or os.environ.get("FRIDGE_LAN_SUBNET")))
    if not hosts:
        return []
    found = []

    def check(ip):
        return ip if _tcp_open(ip, 6668, 0.4) else None

    try:
        with ThreadPoolExecutor(max_workers=32) as pool:
            for ip in pool.map(check, hosts):
                if ip:
                    found.append(ip)
    except Exception:
        pass
    return found

def discover_plug_ip(cfg):
    """Keep using a live TCP/6668 address; rediscover by MAC if DHCP moved the plug."""
    global _last_ip_scan
    saved = (cfg.get("device_ip") or "").strip()
    mac = _norm_mac(cfg.get("device_mac"))
    if saved and _tcp_open(saved):
        return saved
    for ip, hw in _arp_pairs():
        if mac and _norm_mac(hw) == mac and _tcp_open(ip):
            return ip
    now = time.time()
    if now - _last_ip_scan < 45:
        return saved
    _last_ip_scan = now
    hits = _scan_tuya_port(cfg)
    if mac:
        arp = dict(_arp_pairs())
        for ip in hits:
            if _norm_mac(arp.get(ip, "")) == mac:
                return ip
    if saved in hits:
        return saved
    if len(hits) == 1:
        return hits[0]
    return saved

def _close_local():
    global _local_dev, _local_ident
    dev = _local_dev
    _local_dev = None
    _local_ident = None
    if dev is None:
        return
    try:
        dev.close()
    except Exception:
        pass

def _remember_plug(cfg, ip, ver):
    changed = False
    if ip and cfg.get("device_ip") != ip:
        cfg["device_ip"] = ip
        changed = True
    if ver and float(cfg.get("local_version") or 0) != float(ver):
        cfg["local_version"] = float(ver)
        changed = True
    if changed:
        try:
            save_config(cfg)
        except Exception:
            pass

def _local_status(cfg):
    """One short local read. Protocol 3.5 on this plug; never block 30s on 3.4."""
    global _local_dev, _local_ident
    loc_key = (cfg.get("local_key") or "").strip()
    dev_id = (cfg.get("device_id") or "").strip()
    if not (loc_key and dev_id):
        return None
    _ensure_lan_route()
    ip = discover_plug_ip(cfg)
    if not ip:
        return None
    ver = float(cfg.get("local_version") or 3.5)
    ident = (dev_id, ip, loc_key, ver)
    if _local_dev is None or _local_ident != ident:
        _close_local()
        d = tinytuya.OutletDevice(
            dev_id,
            ip,
            loc_key,
            version=ver,
            persist=True,
            connection_timeout=2,
            connection_retry_limit=1,
            connection_retry_delay=0,
        )
        d.set_retry(False)
        _local_dev = d
        _local_ident = ident
    data = _local_dev.status()
    if not (isinstance(data, dict) and "dps" in data):
        _close_local()
        return None
    dps = data.get("dps") or {}
    _remember_plug(cfg, ip, ver)
    return {
        "ok": True,
        "source": "local_wifi",
        "p_raw": float(dps.get("19", dps.get(19, 0)) or 0),
        "v_raw": float(dps.get("20", dps.get(20, 0)) or 0),
        "i_raw": float(dps.get("18", dps.get(18, 0)) or 0),
    }

def _cloud_error_text(res):
    if not isinstance(res, dict):
        return "Tuya Cloud request error"
    return res.get("msg") or res.get("Payload") or res.get("Error") or "Tuya Cloud request error"

def fetch_device_telemetry(cfg):
    """Local Tuya on the LAN first; Cloud only if the plug socket is down."""
    global _local_fail_until
    now = time.time()
    if now >= _local_fail_until:
        try:
            local = _local_status(cfg)
            if local:
                _local_fail_until = 0.0
                return local
        except Exception:
            pass
        _close_local()
        _local_fail_until = now + 20

    c = get_cloud_client(cfg)
    res = c.getstatus(cfg.get("device_id", "").strip())
    record_cloud_call(1)
    if res and isinstance(res, dict) and "result" in res:
        p_raw, v_raw, i_raw = 0, 0, 0
        for item in res.get("result", []):
            code = item.get("code", "")
            val = item.get("value", 0)
            if code in ["cur_power", "cur_power_a", "phase_a_active_power", "active_power"]:
                p_raw = float(val)
            elif code in ["cur_voltage", "cur_voltage_a", "voltage"]:
                v_raw = float(val)
            elif code in ["cur_current", "cur_current_a", "current"]:
                i_raw = float(val)
        return {
            "ok": True,
            "source": "tuya_cloud",
            "p_raw": p_raw,
            "v_raw": v_raw,
            "i_raw": i_raw,
        }

    return {
        "ok": False,
        "msg": _cloud_error_text(res),
    }

def prune_old_measurements(retention_days=7):
    """Prunes raw 4-second telemetry older than retention_days to keep SQLite light on TV Box"""
    try:
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("DELETE FROM measurements WHERE timestamp < ?", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        if deleted > 0:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Auto-pruned {deleted} old raw measurements (older than {retention_days} days).")
    except Exception as e:
        print(f"Prune error: {e}")

def tuya_poller():
    global state
    cycle_powers = []
    cycle_voltages = []
    cycle_currents = []
    warning_long_sent = False
    poll_count = 0
    active_streak = 0
    idle_streak = 0
    ON_STREAK = 2
    OFF_STREAK = 2
    cloud_ok = False
    
    cfg = load_config()
    db_last_end = get_latest_end_time()
    if db_last_end:
        state["rest_start_time"] = db_last_end
    elif cfg.get("last_stop_time"):
        state["rest_start_time"] = cfg["last_stop_time"]
    state["is_running"] = False
    state["cycle_start_time"] = None
    apply_idle_rest_clock()
    check_cold_boot_blackout()
    update_last_blackout_state()
    prune_old_measurements(7)
    
    while True:
        cfg = load_config()
        if not (cfg.get("api_key") and cfg.get("api_secret") and cfg.get("device_id")):
            state["connected"] = False
            state["error_message"] = "Waiting for Tuya API credentials in config.json"
            time.sleep(3)
            continue
            
        poll_count += 1
        if (not cloud_ok and poll_count % 5 == 0) or poll_count % CLOUD_SYNC_EVERY_POLLS == 0:
            if sync_cloud_history():
                cloud_ok = True
        if poll_count % PRUNE_EVERY_POLLS == 0:
            prune_old_measurements(7)
            
        sleep_sec = 16
        try:
            tele = fetch_device_telemetry(cfg)
            
            if tele.get("ok"):
                state["connected"] = True
                state["error_message"] = ""
                state["connection_source"] = tele.get("source", "tuya_cloud")
                
                p_raw = tele.get("p_raw", 0)
                v_raw = tele.get("v_raw", 0)
                i_raw = tele.get("i_raw", 0)
                
                # Tuya standard DPS: 19 is 0.1 W, 20 is 0.1 V, 18 is mA
                power = p_raw / 10.0 if p_raw > 0 else 0.0
                voltage = v_raw / 10.0 if v_raw > 0 else 0.0
                current = i_raw / 1000.0 if i_raw > 0 else 0.0
                
                state["power"] = round(power, 1)
                state["voltage"] = round(voltage, 1)
                state["current"] = round(current, 2)
                state["last_update"] = datetime.now().strftime("%H:%M:%S")
                
                # Optional Tuya probe is treated as freezer-only; fridge stays a model.
                temp_sensor_id = cfg.get("temp_sensor_id", "").strip()
                state["temp_sensor_id"] = temp_sensor_id
                if temp_sensor_id and poll_count % 5 == 0:
                    try:
                        c = get_cloud_client(cfg)
                        t_res = c.getstatus(temp_sensor_id)
                        if t_res and "result" in t_res:
                            for t_item in t_res.get("result", []):
                                t_code = t_item.get("code", "")
                                t_val = float(t_item.get("value", 0))
                                if t_code in ["va_temperature", "temp_current", "temperature"]:
                                    real_t = t_val / 10.0 if t_val > 100 else t_val
                                    state["temp_freezer"] = round(real_t, 1)
                                    state["temp_sensor_connected"] = True
                    except Exception:
                        state["temp_sensor_connected"] = False
                elif not temp_sensor_id:
                    state["temp_sensor_connected"] = False

                state["temp_freezer_estimated"] = not state.get("temp_sensor_connected", False)
                state["temp_fridge_estimated"] = True
                state["temp_is_estimated"] = state["temp_freezer_estimated"]
                
                now_iso = datetime.now().isoformat()
                is_hot = power > 35.0
                if is_hot:
                    active_streak += 1
                    idle_streak = 0
                else:
                    idle_streak += 1
                    active_streak = 0

                last_end = get_latest_end_time() or ""
                try:
                    conn_b = db_connect()
                    cur_b = conn_b.cursor()
                    cur_b.execute("SELECT MAX(end_time) FROM blackouts WHERE end_time > ?", (last_end,))
                    b_row = cur_b.fetchone()
                    if b_row and b_row[0] and b_row[0] > last_end:
                        last_end = b_row[0]
                    conn_b.close()
                except Exception:
                    pass
                restored = None
                open_start = find_open_cycle_start()
                cfg_start = cfg.get("current_start_time")
                cands = [x for x in (open_start, cfg_start) if x and x > last_end]
                if cands:
                    restored = min(cands)
                
                if not state["is_running"] and (active_streak >= ON_STREAK or (is_hot and restored)):
                    state["is_running"] = True
                    state["cycle_start_time"] = restored or now_iso
                    state["cycle_duration_sec"] = 0
                    cycle_powers = []
                    cycle_voltages = []
                    cycle_currents = []
                    warning_long_sent = False
                    cfg_save = load_config()
                    cfg_save["current_start_time"] = state["cycle_start_time"]
                    save_config(cfg_save)
                elif state["is_running"] and restored:
                    cur_start = state.get("cycle_start_time")
                    if not cur_start or restored < cur_start:
                        state["cycle_start_time"] = restored
                        cfg_save = load_config()
                        cfg_save["current_start_time"] = restored
                        save_config(cfg_save)
                
                if state["is_running"] and idle_streak >= OFF_STREAK:
                    duration = state["cycle_duration_sec"]
                    end_iso = now_iso
                    cfg_save = load_config()
                    cfg_save["last_stop_time"] = end_iso
                    cfg_save["current_start_time"] = None
                    save_config(cfg_save)
                    if duration >= 120:
                        avg_p = sum(cycle_powers) / len(cycle_powers) if cycle_powers else 131.0
                        avg_v = sum(cycle_voltages) / len(cycle_voltages) if cycle_voltages else 226.0
                        avg_i = sum(cycle_currents) / len(cycle_currents) if cycle_currents else None
                        c_type = classify_cycle(avg_p, duration, cooling_since_last_defrost_sec(), cycle_powers, voltage=avg_v, current=avg_i)
                        conn = db_connect()
                        cur = conn.cursor()
                        cur.execute('''
                            INSERT OR IGNORE INTO cycles (start_time, end_time, duration_sec, avg_power, avg_voltage, cycle_type)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (state["cycle_start_time"], end_iso, duration, round(avg_p, 1), round(avg_v, 1), c_type))
                        conn.commit()
                        conn.close()
                        invalidate_history_cache()
                    
                    state["is_running"] = False
                    state["cycle_start_time"] = None
                    state["cycle_duration_sec"] = 0
                    state["rest_start_time"] = end_iso
                    state["rest_duration_sec"] = 0
                    active_streak = 0
                    idle_streak = 0
                    cycle_powers = []
                    cycle_voltages = []
                    cycle_currents = []
                    
                if state["is_running"]:
                    try:
                        st_dt = datetime.fromisoformat(state["cycle_start_time"])
                        state["cycle_duration_sec"] = max(0, int((datetime.now() - st_dt).total_seconds()))
                    except Exception:
                        state["cycle_duration_sec"] = 0
                        
                    cycle_powers.append(power)
                    cycle_voltages.append(voltage)
                    cycle_currents.append(current)
                    state["rest_duration_sec"] = 0
                    update_restart_lockout(True)
                    
                    live_avg_p = sum(cycle_powers) / len(cycle_powers) if cycle_powers else power
                    c_type = classify_cycle(live_avg_p, state["cycle_duration_sec"], cooling_since_last_defrost_sec(), cycle_powers, voltage=voltage, current=current)
                    if c_type == "defrost":
                        state["current_mode"] = "defrost"
                        state["mode_title"] = "🔥 АВТООТТАЙКА NO FROST (ТЭН)"
                    else:
                        state["current_mode"] = "cooling"
                        state["mode_title"] = "🟢 КОМПРЕССОР РАБОТАЕТ (ОХЛАЖДЕНИЕ)"
                    
                    ez, er = estimated_chamber_temps(True, state["cycle_duration_sec"])
                    if state.get("temp_freezer_estimated", True):
                        state["temp_freezer"] = ez
                    if state.get("temp_fridge_estimated", True):
                        state["temp_fridge"] = er
                        
                    if state["cycle_duration_sec"] > LONG_RUN_WARN_SEC and not warning_long_sent:
                        warning_long_sent = True
                        send_pc_toast(
                            "⚠️ Длительная непрерывная работа",
                            "Холодильник работает уже более 7 часов подряд без остановки. Проверьте дверь, уплотнители и уровень фреона.",
                            is_warning=True
                        )
                else:
                    state["current_mode"] = "idle"
                    state["mode_title"] = "⚪ ПОЛНЫЙ ПОКОЙ (ОТДЫХ)"
                    apply_idle_rest_clock()
                    update_restart_lockout(False)
                    ez, er = estimated_chamber_temps(False, state["rest_duration_sec"])
                    if state.get("temp_freezer_estimated", True):
                        state["temp_freezer"] = ez
                    if state.get("temp_fridge_estimated", True):
                        state["temp_fridge"] = er
                
                try:
                    conn = db_connect()
                    cur = conn.cursor()

                    # Proactive cold-boot / blackout gap detection before saving new measurement
                    cur.execute("SELECT timestamp, power, voltage FROM measurements ORDER BY id DESC LIMIT 1")
                    last_m = cur.fetchone()
                    if last_m and last_m[0]:
                        prev_ts = parse_iso(last_m[0])
                        curr_ts = parse_iso(now_iso)
                        if prev_ts and curr_ts:
                            gap_sec = (curr_ts - prev_ts).total_seconds()
                            uptime_sec = 999999.0
                            try:
                                with open("/proc/uptime", "r") as f:
                                    uptime_sec = float(f.read().split()[0])
                            except Exception:
                                pass
                            is_cold_boot = (uptime_sec <= 900)
                            if gap_sec >= 1800 and is_cold_boot:
                                # 1. Close unclosed cycle prior to the blackout
                                cfg_c = load_config()
                                c_start = cfg_c.get("current_start_time")
                                if c_start and c_start < last_m[0]:
                                    c_st_dt = parse_iso(c_start)
                                    if c_st_dt:
                                        c_dur = int((prev_ts - c_st_dt).total_seconds())
                                        if c_dur >= 60:
                                            cur.execute("""
                                                INSERT OR IGNORE INTO cycles (start_time, end_time, duration_sec, avg_power, avg_voltage, cycle_type)
                                                VALUES (?, ?, ?, ?, ?, ?)
                                            """, (c_start, last_m[0], c_dur, float(last_m[1] or 135.0), float(last_m[2] or 210.0), "cooling"))
                                cfg_c["current_start_time"] = None
                                save_config(cfg_c)

                                # 2. Auto-record blackout into database
                                safety = food_safety_label(gap_sec)
                                cur.execute("""
                                    INSERT OR IGNORE INTO blackouts (start_time, end_time, duration_sec, food_safety_status)
                                    VALUES (?, ?, ?, ?)
                                """, (last_m[0], now_iso, int(gap_sec), safety))
                                conn.commit()
                                update_last_blackout_state()
                                if _telegram_bot:
                                    try:
                                        _telegram_bot.notify_blackout_resolved(last_m[0], now_iso, int(gap_sec), safety)
                                    except Exception:
                                        pass

                    tf = None if state.get("temp_freezer_estimated", True) else state.get("temp_freezer")
                    tr = None if state.get("temp_fridge_estimated", True) else state.get("temp_fridge")
                    cur.execute('''
                        INSERT INTO measurements (timestamp, power, voltage, current, is_running, mode, temp_freezer, temp_fridge)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (now_iso, state["power"], state["voltage"], state["current"], 1 if state["is_running"] else 0, state["current_mode"], tf, tr))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
                
            else:
                state["connected"] = False
                state["connection_source"] = ""
                state["error_message"] = tele.get("msg", "Tuya request error")
                sleep_sec = 10
                
        except Exception as e:
            state["connected"] = False
            state["connection_source"] = ""
            state["error_message"] = str(e)
            sleep_sec = 10

        # Hourly cloud energy reconciliation in detached background thread (XX:05)
        now_dt = datetime.now()
        global _last_hourly_reconcile_hour
        if now_dt.minute >= 5 and now_dt.hour != _last_hourly_reconcile_hour:
            _last_hourly_reconcile_hour = now_dt.hour
            def _bg_hourly():
                try:
                    if now_dt.hour == 0:
                        y_str = (now_dt - timedelta(days=1)).strftime("%Y-%m-%d")
                        reconcile_energy_with_cloud(y_str, force=True)
                    reconcile_energy_with_cloud(now_dt.strftime("%Y-%m-%d"), force=True)
                except Exception as err:
                    print("Hourly reconciliation notice:", err)
            threading.Thread(target=_bg_hourly, daemon=True).start()

        if _telegram_bot:
            try:
                _telegram_bot.check_watchdogs()
            except Exception:
                pass

        time.sleep(max(2, int(sleep_sec)))

@app.before_request
def _lan_only():
    if not is_lan_request():
        return jsonify({"error": "Forbidden: LAN-only service"}), 403
    if request.method == "OPTIONS":
        res = app.make_default_options_response()
        res.headers["Access-Control-Allow-Origin"] = "*"
        res.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        res.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return res

@app.after_request
def add_cache_and_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")

@app.route("/ipad")
def ipad():
    return send_from_directory(STATIC_DIR, "ipad.html")

@app.route("/api/status")
def get_status():
    now = datetime.now()
    if state.get("is_running") and state.get("cycle_start_time"):
        try:
            st_dt = parse_iso(state["cycle_start_time"])
            if st_dt:
                state["cycle_duration_sec"] = max(0, int((now - st_dt).total_seconds()))
        except Exception:
            pass
    elif not state.get("is_running") and state.get("rest_start_time"):
        try:
            st_dt = parse_iso(state["rest_start_time"])
            if st_dt:
                state["rest_duration_sec"] = max(0, int((now - st_dt).total_seconds()))
        except Exception:
            pass
    res_state = state.snapshot()
    res_state["cloud_quota"] = get_cloud_quota_stats()
    return jsonify(res_state)

@app.route("/api/toggle_notifications", methods=["POST"])
def toggle_notifications():
    if not is_localhost_request():
        return jsonify({"error": "Forbidden: Localhost access only"}), 403
    data = request.json or {}
    state["notifications_enabled"] = bool(data.get("enabled", True))
    return jsonify({"notifications_enabled": state["notifications_enabled"]})

@app.route("/api/test_notification", methods=["POST"])
def test_notification():
    if not is_localhost_request():
        return jsonify({"error": "Forbidden: Localhost access only"}), 403
    send_pc_toast("🔔 Test Notification", "SmartFridge notifications are working!")
    return jsonify({"status": "sent"})

@app.route("/api/config", methods=["GET", "POST"])
def config_api():
    """Secure Config Endpoint: Masks secrets on GET, restricts POST to LAN"""
    if request.method == "POST":
        if not is_lan_request():
            return jsonify({"error": "Forbidden: LAN access only"}), 403
        data = request.json or {}
        cfg = load_config()
        if data.get("api_region"):
            cfg["api_region"] = data["api_region"].strip()
        if data.get("api_key") and "..." not in data["api_key"] and data["api_key"] != "********":
            cfg["api_key"] = data["api_key"].strip()
        if data.get("api_secret") and data["api_secret"] != "********":
            cfg["api_secret"] = data["api_secret"].strip()
        if data.get("device_id") and "..." not in data["device_id"] and data["device_id"] != "********":
            cfg["device_id"] = data["device_id"].strip()
        if "temp_sensor_id" in data:
            cfg["temp_sensor_id"] = data["temp_sensor_id"].strip()
        if data.get("lan_subnet"):
            cfg["lan_subnet"] = str(parse_lan_network(data["lan_subnet"]))
        if "telegram_bot_token" in data and "..." not in data["telegram_bot_token"] and data["telegram_bot_token"] != "********":
            cfg["telegram_bot_token"] = data["telegram_bot_token"].strip()
        if "telegram_chat_id" in data:
            cfg["telegram_chat_id"] = str(data["telegram_chat_id"]).strip()
        if "telegram_enabled" in data:
            cfg["telegram_enabled"] = bool(data["telegram_enabled"])
        save_config(cfg)
        if _telegram_bot:
            _telegram_bot.update_credentials(
                cfg.get("telegram_bot_token", ""),
                cfg.get("telegram_chat_id", ""),
                cfg.get("telegram_enabled", False)
            )
        return jsonify({"status": "saved"})
    
    cfg = load_config()
    is_local = is_localhost_request()
    raw_key = cfg.get("api_key", "")
    raw_dev = cfg.get("device_id", "")
    raw_tg = cfg.get("telegram_bot_token", "")
    
    safe_key = raw_key if is_local else (raw_key[:4] + "..." + raw_key[-4:] if len(raw_key) > 8 else "***")
    safe_dev = raw_dev if is_local else (raw_dev[:4] + "..." + raw_dev[-4:] if len(raw_dev) > 8 else "***")
    safe_tg = raw_tg if is_local else (raw_tg[:4] + "..." + raw_tg[-4:] if len(raw_tg) > 8 else "***") if raw_tg else ""
    
    safe_cfg = {
        "api_region": cfg.get("api_region", "eu"),
        "api_key": safe_key,
        "api_secret": "********" if cfg.get("api_secret") else "",
        "device_id": safe_dev,
        "temp_sensor_id": cfg.get("temp_sensor_id", ""),
        "lan_subnet": cfg.get("lan_subnet", "192.168.0.0/24"),
        "telegram_bot_token": safe_tg,
        "telegram_chat_id": cfg.get("telegram_chat_id", ""),
        "telegram_enabled": bool(cfg.get("telegram_enabled", False))
    }
    return jsonify(safe_cfg)

@app.route("/api/test_telegram", methods=["POST"])
def test_telegram_api():
    if not is_lan_request():
        return jsonify({"error": "Forbidden: LAN access only"}), 403
    data = request.json or {}
    cfg = load_config()
    token = str(data.get("telegram_bot_token") or cfg.get("telegram_bot_token", "")).strip()
    chat_id = str(data.get("telegram_chat_id") or cfg.get("telegram_chat_id", "")).strip()
    if not token or not chat_id:
        return jsonify({"success": False, "error": "Токен или Chat ID не заданы в конфигурации"}), 400

    global _telegram_bot
    if not _telegram_bot and HAS_TELEGRAM:
        _telegram_bot = FridgeTelegramBot(
            token=token,
            chat_id=chat_id,
            enabled=True,
            state_ref=state,
            db_connect_fn=db_connect,
            reconcile_fn=reconcile_energy_with_cloud,
            config_fn=load_config
        )
    elif _telegram_bot:
        _telegram_bot.update_credentials(token, chat_id, True)

    if _telegram_bot:
        target_ids = FridgeTelegramBot._parse_chat_ids(chat_id) if HAS_TELEGRAM else [chat_id]
        success_count = 0
        last_err = ""
        for cid in target_ids:
            ok = _telegram_bot.send_message(
                "🔔 <b>Тест связи с Telegram!</b>\n\nМонитор холодильника Samsung RT34MB успешно подключён к вашему чату.",
                chat_id=cid,
                reply_markup=DEFAULT_REPLY_KEYBOARD
            )
            if ok:
                success_count += 1
            else:
                last_err = _telegram_bot.last_error_desc

        if success_count == len(target_ids):
            msg = "Тестовое сообщение успешно отправлено!" if len(target_ids) == 1 else f"Тестовые сообщения отправлены ({success_count} получателям)!"
            return jsonify({"success": True, "message": msg})
        elif success_count > 0:
            return jsonify({"success": True, "message": f"Отправлено {success_count} из {len(target_ids)} чатов. Ошибка: {last_err}"})

        err_msg = last_err or "Не удалось отправить сообщение"
        if "chat not found" in err_msg.lower():
            err_msg = "Чат не найден! Убедитесь, что все пользователи открыли бота и нажали 'Запустить' (/start)"
        return jsonify({"success": False, "error": err_msg}), 400
    return jsonify({"success": False, "error": "Модуль Telegram недоступен"}), 500

def build_live_journal_row():
    """Open rest/work so the journal is not stuck on the last finished cycle."""
    now = datetime.now()
    if state.get("is_running") and state.get("cycle_start_time"):
        cs = parse_iso(state.get("cycle_start_time"))
        if not cs:
            return None
        dur = int(state.get("cycle_duration_sec") or 0)
        c_type = state.get("current_mode") if state.get("current_mode") in ("defrost", "cooling") else "cooling"
        return {
            "full_end": now.isoformat(),
            "date": cs.strftime("%d.%m.%Y"),
            "date_short": cs.strftime("%d.%m"),
            "start": cs.strftime("%H:%M"),
            "end": "сейчас",
            "duration_sec": dur,
            "duration_str": format_gap_str(max(dur, 1)) or "1 мин",
            "rest_sec": 0,
            "rest_str": "ещё работает",
            "rest_start": "—",
            "rest_end": "—",
            "krv": "—",
            "avg_power": state.get("power") or 0,
            "avg_voltage": state.get("voltage") or 0,
            "cycle_type": c_type,
            "live": True
        }
    return None

_history_cache = {
    "cycles": [],
    "blackouts": [],
    "summary": {},
    "last_built": 0.0,
    "version": 0
}

_analytics_cache = {
    "data": None,
    "last_built": 0.0,
    "tariff": None,
    "currency": None
}

def invalidate_history_cache():
    _history_cache["last_built"] = 0.0
    _analytics_cache["last_built"] = 0.0

def build_completed_history_cache():
    conn = db_connect()
    cur = conn.cursor()
    
    # One row per cycles record
    cur.execute("""
        SELECT
            start_time,
            end_time,
            duration_sec,
            avg_power,
            avg_voltage,
            cycle_type
        FROM cycles
        WHERE duration_sec >= 120
        ORDER BY start_time ASC, id ASC
    """)
    raw_cycles = merge_micro_cycles(cur.fetchall())
    
    # Blackouts
    cur.execute("SELECT start_time, end_time, duration_sec, food_safety_status FROM blackouts ORDER BY start_time DESC LIMIT 20")
    raw_blackouts = cur.fetchall()
    conn.close()
    
    enhanced_cycles = []
    total_work_sec = 0
    total_rest_sec = 0
    
    for i in range(len(raw_cycles)):
        c = raw_cycles[i]
        dur = c[2]
        c_type = c[5] if len(c) > 5 and c[5] else "cooling"
        
        rest_sec = 0
        rest_str = "—"
        rest_start = "—"
        rest_end = "—"
        krv_val = "—"
        
        dt_st = parse_iso(c[0])
        dt_end = parse_iso(c[1])
        if dt_st:
            date_full = dt_st.strftime("%d.%m.%Y")
            date_short = dt_st.strftime("%d.%m")
            s_time = dt_st.strftime("%H:%M")
            e_time = dt_end.strftime("%H:%M") if dt_end else "--:--"
        else:
            date_full = datetime.now().strftime("%d.%m.%Y")
            date_short = datetime.now().strftime("%d.%m")
            s_time = "--:--"
            e_time = "--:--"
        
        if dt_end:
            next_st = None
            next_label = None
            for j in range(i + 1, len(raw_cycles)):
                cand = parse_iso(raw_cycles[j][0])
                if cand and cand >= dt_end:
                    next_st = cand
                    next_label = cand.strftime("%H:%M")
                    break
            if next_st is None and i == len(raw_cycles) - 1:
                if state.get("is_running") and state.get("cycle_start_time"):
                    cs = parse_iso(state.get("cycle_start_time"))
                    if cs and cs >= dt_end:
                        next_st = cs
                        next_label = cs.strftime("%H:%M")
                elif not state.get("is_running"):
                    next_st = datetime.now()
                    next_label = "сейчас"
            rest_sec, rest_str, rest_start, rest_end, krv_val = fill_rest_after(
                dt_end, next_st, next_label, raw_blackouts, dur if c_type == "cooling" else 0
            )
            if rest_sec and c_type == "cooling":
                total_rest_sec += rest_sec
            if c_type != "cooling":
                krv_val = "—"

        if c_type == "cooling":
            total_work_sec += dur
            
        enhanced_cycles.append({
            "full_end": c[1] or "",
            "date": date_full,
            "date_short": date_short,
            "start": s_time,
            "end": e_time,
            "duration_sec": dur,
            "duration_str": format_gap_str(dur) or "—",
            "rest_sec": rest_sec,
            "rest_str": rest_str,
            "rest_start": rest_start,
            "rest_end": rest_end,
            "krv": krv_val,
            "avg_power": c[3],
            "avg_voltage": c[4],
            "cycle_type": c_type
        })
    
    formatted_blackouts = []
    seen_blackouts = set()
    for b in raw_blackouts:
        st_iso, end_iso, dur_sec, _stored_safety = b
        key = (st_iso[:16], end_iso[:16])
        if key in seen_blackouts:
            continue
        seen_blackouts.add(key)
        try:
            dt_st = datetime.fromisoformat(st_iso)
            dt_end = datetime.fromisoformat(end_iso)
            dur_str = format_gap_str(dur_sec) or "—"
            safety = food_safety_label(dur_sec)
            b_item = {
                "date": dt_st.strftime("%d.%m.%Y"),
                "start": dt_st.strftime("%H:%M"),
                "end": dt_end.strftime("%H:%M"),
                "duration_sec": dur_sec,
                "duration_str": dur_str,
                "safety": safety
            }
            formatted_blackouts.append(b_item)
            
            enhanced_cycles.append({
                "full_end": end_iso,
                "date": dt_st.strftime("%d.%m.%Y"),
                "date_short": dt_st.strftime("%d.%m"),
                "start": dt_st.strftime("%H:%M"),
                "end": dt_end.strftime("%H:%M"),
                "duration_sec": dur_sec,
                "duration_str": dur_str,
                "rest_sec": 0,
                "rest_str": "Сеть 0V",
                "rest_start": "—",
                "rest_end": "—",
                "krv": "—",
                "avg_power": 0.0,
                "avg_voltage": 0.0,
                "cycle_type": "blackout",
                "safety": safety
            })
        except Exception:
            pass

    # Sort all events chronologically (newest first)
    enhanced_cycles.sort(key=lambda x: str(x.get("full_end", "")), reverse=True)

    overall_krv = duty_cycle(total_work_sec, total_rest_sec) or 0.38

    _history_cache["cycles"] = enhanced_cycles
    _history_cache["blackouts"] = formatted_blackouts
    _history_cache["summary"] = {
        "overall_krv": overall_krv,
        "total_cycles": len(enhanced_cycles),
        "krv_status": "🟢 Отличный (энергоэффективный)" if overall_krv <= 0.50 else "🟡 Повышенный"
    }
    _history_cache["last_built"] = time.time()

@app.route("/api/history")
def get_history():
    now = time.time()
    if not _history_cache["cycles"] or (now - _history_cache["last_built"]) > 45.0:
        build_completed_history_cache()
        
    limit_param = request.args.get("limit", "30").strip().lower()
    
    cached_cycles = _history_cache["cycles"]
    cached_blackouts = _history_cache["blackouts"]
    cached_summary = _history_cache["summary"]
    
    total_count = len(cached_cycles)
    
    if limit_param in ("all", "0", "max"):
        cycles_slice = list(cached_cycles)
    else:
        try:
            lim = int(limit_param)
            cycles_slice = list(cached_cycles[:lim]) if lim > 0 else list(cached_cycles)
        except ValueError:
            cycles_slice = list(cached_cycles[:30])
            
    # Prepend dynamic live journal row if active
    live_row = build_live_journal_row()
    if live_row:
        cycles_slice.insert(0, live_row)
        
    summary_out = dict(cached_summary)
    summary_out["total_cycles"] = total_count + (1 if live_row else 0)
    summary_out["returned_cycles"] = len(cycles_slice)
    
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT timestamp, power, voltage, current, is_running, temp_freezer, temp_fridge FROM measurements ORDER BY id DESC LIMIT 50")
        m_rows = cur.fetchall()
        conn.close()
    except Exception:
        m_rows = []
        
    return jsonify({
        "measurements": [
            {
                "time": r[0][11:16], 
                "power": r[1], 
                "voltage": r[2], 
                "current": r[3], 
                "is_running": bool(r[4]),
                "temp_freezer": r[5] if len(r) > 5 else None,
                "temp_fridge": r[6] if len(r) > 6 else None,
            }
            for r in reversed(m_rows)
        ],
        "cycles": cycles_slice,
        "blackouts": cached_blackouts,
        "summary": summary_out
    })

def build_analytics_cache(tariff=None, currency=None):
    cfg = load_config()
    default_tariff = float(cfg.get("electricity_tariff", 5.0))
    try:
        current_tariff = float(tariff) if tariff is not None else default_tariff
        if current_tariff < 0 or current_tariff > 1000.0 or str(current_tariff) in ("nan", "inf", "-inf"):
            current_tariff = default_tariff
    except (ValueError, TypeError):
        current_tariff = default_tariff

    current_currency = str(currency).strip()[:8] if currency is not None else str(cfg.get("currency", "₽")).strip()[:8]
    if not current_currency:
        current_currency = "₽"

    try:
        conn = db_connect()
        cur = conn.cursor()

        # 1. Daily timeline from cycles (up to last 30 days)
        cur.execute("""
            SELECT 
                date(start_time) as day,
                count(*) as total_cycles,
                sum(case when cycle_type = 'cooling' then duration_sec else 0 end) as cool_sec,
                sum(case when cycle_type = 'defrost' then duration_sec else 0 end) as defrost_sec,
                sum(case when cycle_type = 'cooling' then (avg_power * duration_sec / 3600.0 / 1000.0) else 0 end) as cool_kwh,
                sum(case when cycle_type = 'defrost' then (avg_power * duration_sec / 3600.0 / 1000.0) else 0 end) as defrost_kwh,
                round(avg(avg_voltage), 1) as avg_v,
                round(min(avg_voltage), 1) as min_v,
                round(avg(case when cycle_type = 'cooling' then duration_sec else null end) / 60.0, 1) as avg_cool_min
            FROM cycles
            WHERE duration_sec >= 120
            GROUP BY day
            ORDER BY day ASC
        """)
        daily_raw = cur.fetchall()

        timeline = []
        total_cool_kwh = 0.0
        total_defrost_kwh = 0.0
        total_cool_sec = 0.0
        total_cycles_all = 0

        for r in daily_raw:
            d_day = r[0]
            d_cycles = r[1]
            c_sec = r[2] or 0
            df_sec = r[3] or 0
            c_kwh = r[4] or 0.0
            df_kwh = r[5] or 0.0
            tot_kwh = c_kwh + df_kwh
            avg_v = r[6] or 220.0
            min_v = r[7] or 220.0
            avg_cool_min = r[8] or 0.0

            total_cool_kwh += c_kwh
            total_defrost_kwh += df_kwh
            total_cool_sec += c_sec
            total_cycles_all += d_cycles

            day_krv = round(c_sec / 86400.0, 2)

            timeline.append({
                "date": d_day,
                "date_short": d_day[5:].replace("-", "."),
                "cycles": d_cycles,
                "cooling_hours": round(c_sec / 3600.0, 1),
                "kwh": round(tot_kwh, 2),
                "cool_kwh": round(c_kwh, 2),
                "defrost_kwh": round(df_kwh, 2),
                "krv": day_krv,
                "avg_voltage": avg_v,
                "min_voltage": min_v,
                "avg_cycle_min": avg_cool_min
            })

        days_count = max(len(timeline), 1)
        all_kwh = total_cool_kwh + total_defrost_kwh
        daily_avg_kwh = round(all_kwh / days_count, 2)
        monthly_kwh_forecast = round(daily_avg_kwh * 30.5, 1)

        daily_cost = round(daily_avg_kwh * current_tariff, 2)
        monthly_cost_forecast = round(monthly_kwh_forecast * current_tariff, 2)

        # 2. Defrost analytics
        cur.execute("""
            SELECT 
                count(*),
                round(avg(duration_sec)/60.0, 1),
                round(min(duration_sec)/60.0, 1),
                round(max(duration_sec)/60.0, 1),
                round(avg(avg_power), 1),
                sum(avg_power * duration_sec / 3600.0 / 1000.0)
            FROM cycles
            WHERE cycle_type = 'defrost'
        """)
        df_row = cur.fetchone()
        df_count = df_row[0] or 0
        df_avg_min = df_row[1] or 0.0
        df_min_min = df_row[2] or 0.0
        df_max_min = df_row[3] or 0.0
        df_avg_pwr = df_row[4] or 0.0
        df_tot_kwh = df_row[5] or 0.0

        avg_defrost_interval_hours = round((total_cool_sec / 3600.0) / max(df_count, 1), 1)

        # 3. Voltage Risk Analytics
        cur.execute("""
            SELECT 
                count(*),
                sum(case when avg_voltage < 190 then 1 else 0 end),
                sum(case when avg_voltage < 190 then duration_sec else 0 end) / 3600.0,
                sum(case when avg_voltage >= 190 and avg_voltage < 205 then 1 else 0 end),
                sum(case when avg_voltage >= 205 and avg_voltage < 215 then 1 else 0 end),
                sum(case when avg_voltage >= 215 then 1 else 0 end)
            FROM cycles
            WHERE cycle_type = 'cooling' AND duration_sec >= 120
        """)
        v_row = cur.fetchone()
        total_cool_cycles = max(v_row[0] or 0, 1)
        v_red_count = v_row[1] or 0
        v_red_hours = round(v_row[2] or 0.0, 1)
        v_low_count = v_row[3] or 0
        v_moderate_count = v_row[4] or 0
        v_norm_count = v_row[5] or 0

        # 4. Hourly Activity Profile (last 7 days)
        cur.execute("""
            SELECT 
                substr(timestamp, 12, 2) as hr,
                count(*) as total_samples,
                sum(case when is_running = 1 then 1 else 0 end) as run_samples,
                round(avg(voltage), 1) as avg_v
            FROM measurements
            WHERE timestamp >= date('now', '-7 days')
            GROUP BY hr
            ORDER BY hr ASC
        """)
        hourly_raw = cur.fetchall()
        conn.close()

        hourly_profile = []
        for h in hourly_raw:
            tot = h[1]
            run = h[2]
            v = h[3]
            pct = round((run * 100.0 / tot), 1) if tot > 0 else 0.0
            hourly_profile.append({
                "hour": h[0] + ":00",
                "hour_num": int(h[0]),
                "duty_pct": pct,
                "avg_voltage": v
            })

        # Compressor Health Score (out of 100)
        health_score = 100
        overall_krv = round(total_cool_sec / (days_count * 86400.0), 2)
        if overall_krv > 0.50:
            health_score -= 15
        elif overall_krv > 0.45:
            health_score -= 8
        health_score -= min(int(v_red_hours * 5), 20)
        if df_avg_min > 28:
            health_score -= 10
        health_score = max(health_score, 50)

        result = {
            "energy": {
                "total_kwh": round(all_kwh, 2),
                "compressor_kwh": round(total_cool_kwh, 2),
                "defrost_kwh": round(total_defrost_kwh, 2),
                "defrost_share_pct": round(total_defrost_kwh * 100.0 / max(all_kwh, 0.01), 1),
                "daily_avg_kwh": daily_avg_kwh,
                "monthly_forecast_kwh": monthly_kwh_forecast,
                "tariff": current_tariff,
                "currency": current_currency,
                "daily_cost": daily_cost,
                "monthly_cost": monthly_cost_forecast,
                "days_monitored": days_count,
                "reconciliation": state.get("last_energy_reconciliation") or get_latest_reconciliation()
            },
            "voltage_health": {
                "red_cycles_count": v_red_count,
                "red_hours": v_red_hours,
                "low_cycles_count": v_low_count,
                "moderate_cycles_count": v_moderate_count,
                "norm_cycles_count": v_norm_count,
                "total_cycles": total_cool_cycles,
                "norm_pct": round(v_norm_count * 100.0 / total_cool_cycles, 1)
            },
            "defrost_health": {
                "total_count": df_count,
                "avg_duration_min": df_avg_min,
                "min_duration_min": df_min_min,
                "max_duration_min": df_max_min,
                "avg_power_w": df_avg_pwr,
                "avg_interval_hours": avg_defrost_interval_hours,
                "status": "🟢 Отлично" if 20 <= df_avg_min <= 28 else "🟡 Проверить"
            },
            "health_score": health_score,
            "daily_timeline": timeline[-14:],
            "hourly_profile": hourly_profile
        }
        _analytics_cache["data"] = result
        _analytics_cache["last_built"] = time.time()
        _analytics_cache["tariff"] = current_tariff
        _analytics_cache["currency"] = current_currency
        return result
    except Exception as e:
        print(f"Error building analytics: {e}")
        return {
            "error": str(e),
            "energy": {"daily_avg_kwh": 0, "monthly_forecast_kwh": 0, "daily_cost": 0, "monthly_cost": 0, "tariff": current_tariff, "currency": current_currency},
            "voltage_health": {"red_hours": 0, "norm_pct": 100},
            "defrost_health": {"total_count": 0, "avg_duration_min": 0, "status": "—"},
            "health_score": 100,
            "daily_timeline": [],
            "hourly_profile": []
        }

@app.route("/api/analytics")
def get_analytics():
    now = time.time()
    tariff_param = request.args.get("tariff")
    curr_param = request.args.get("currency")
    force = request.args.get("refresh") == "1"

    # Invalidate if tariff changed or refresh requested
    if tariff_param is not None or curr_param is not None or force:
        build_analytics_cache(tariff_param, curr_param)
    elif not _analytics_cache["data"] or (now - _analytics_cache["last_built"]) > 60.0:
        build_analytics_cache()

    return jsonify(_analytics_cache["data"])

@app.route("/api/reconcile-energy", methods=["GET", "POST"])
def reconcile_energy_api():
    if not is_lan_request():
        return jsonify({"error": "Forbidden: LAN access only"}), 403

    if request.method == "POST":
        data = request.json or {}
        target_date = data.get("date")
        res = reconcile_energy_with_cloud(target_date, force=True)
        _analytics_cache["last_built"] = 0.0 # invalidate cache
        return jsonify(res)

    latest = state.get("last_energy_reconciliation") or get_latest_reconciliation()
    history = []
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute('''
            SELECT date_str, local_kwh, cloud_kwh, delta_kwh, accuracy_pct, cloud_reports_count, reconciled_at, status
            FROM energy_reconciliations
            ORDER BY date_str DESC, id DESC LIMIT 7
        ''')
        for r in cur.fetchall():
            history.append({
                "date": r[0],
                "local_kwh": r[1],
                "cloud_kwh": r[2],
                "delta_kwh": r[3],
                "accuracy_pct": r[4],
                "reports_count": r[5],
                "reconciled_at": r[6],
                "status": r[7]
            })
        conn.close()
    except Exception:
        pass
    return jsonify({
        "latest": latest,
        "history": history
    })

@app.route("/api/tariff", methods=["GET", "POST"])
def tariff_api():
    cfg = load_config()
    if request.method == "POST":
        if not is_lan_request():
            return jsonify({"error": "Forbidden: LAN access only"}), 403
        data = request.json or {}
        if "tariff" in data:
            try:
                val = float(data["tariff"])
                if 0.0 <= val <= 1000.0 and str(val) not in ("nan", "inf", "-inf"):
                    cfg["electricity_tariff"] = round(val, 2)
            except (ValueError, TypeError):
                pass
        if "currency" in data and data["currency"]:
            curr_val = str(data["currency"]).strip()[:8]
            if curr_val:
                cfg["currency"] = curr_val
        save_config(cfg)
        _analytics_cache["last_built"] = 0.0 # invalidate cache
        return jsonify({"status": "saved", "tariff": cfg.get("electricity_tariff", 5.0), "currency": cfg.get("currency", "₽")})
    return jsonify({"tariff": cfg.get("electricity_tariff", 5.0), "currency": cfg.get("currency", "₽")})

def start_background_services():
    """DB, optional cloud backfill, and Tuya poller. Not run on import (keeps tests clean)."""
    global _poller_thread
    init_db()
    try:
        state["last_energy_reconciliation"] = get_latest_reconciliation()
    except Exception:
        pass
    try:
        sync_cloud_history()
    except Exception:
        pass
    try:
        reclassify_stored_cycles()
    except Exception:
        pass
    if _poller_thread is None or not _poller_thread.is_alive():
        _poller_thread = threading.Thread(target=tuya_poller, daemon=True)
        _poller_thread.start()
    global _telegram_bot
    if _telegram_bot is None and HAS_TELEGRAM:
        try:
            cfg = load_config()
            _telegram_bot = FridgeTelegramBot(
                token=cfg.get("telegram_bot_token", ""),
                chat_id=cfg.get("telegram_chat_id", ""),
                enabled=cfg.get("telegram_enabled", False),
                state_ref=state,
                db_connect_fn=db_connect,
                reconcile_fn=reconcile_energy_with_cloud,
                config_fn=load_config
            )
            _telegram_bot.start()
        except Exception as err:
            print("Failed to start telegram bot:", err)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SmartFridge Telemetry Kiosk Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host address to bind (default: 0.0.0.0 for LAN)")
    parser.add_argument("--port", type=int, default=8088, help="Port to listen on (default: 8088)")
    args = parser.parse_args()
    try:
        signal.signal(signal.SIGTERM, _on_process_stop)
        signal.signal(signal.SIGINT, _on_process_stop)
    except Exception:
        pass
    os.makedirs(STATIC_DIR, exist_ok=True)
    start_background_services()
    print(f"SmartFridge Server running at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
