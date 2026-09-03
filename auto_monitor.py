import os
import sys
import time
import json
import socket
import sqlite3
import argparse
import threading
import signal
import subprocess
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
import tinytuya

try:
    from winotify import Notification, audio
    HAS_WINOTIFY = True
except ImportError:
    HAS_WINOTIFY = False

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
DB_FILE = os.path.join(APP_DIR, "fridge_data.db")
STATIC_DIR = os.path.join(APP_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR)

# Global Telemetry State with Temperature and Blackout Tracking
state = {
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
    }
}

def is_localhost_request():
    """Checks if request originated from local machine"""
    remote = request.remote_addr
    return remote in ["127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"]

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

def init_db():
    conn = sqlite3.connect(DB_FILE)
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
    conn.commit()
    conn.close()

init_db()

def record_cloud_call(n=1):
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(DB_FILE)
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
        conn = sqlite3.connect(DB_FILE)
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
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "api_region": "eu",
        "api_key": "",
        "api_secret": "",
        "device_id": "",
        "temp_sensor_id": "",
        "local_key": "",
        "device_ip": "",
        "device_mac": "",
        "local_version": 3.5,
        "last_stop_time": None,
        "current_start_time": None
    }

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)

# No Frost timer counts compressor ON time (~8–10 h), then a 10–25 min
# sheath heater at ~160–175 W. SK170K cooling is 125–185 W, so a short
# 158+ W compressor run is not a defrost unless enough cooling has elapsed.
DEFROST_POWER_MIN = 158.0
DEFROST_POWER_MAX = 185.0
DEFROST_MIN_SEC = 480    # 8 min
DEFROST_MAX_SEC = 1680   # 28 min
MIN_COOLING_BEFORE_DEFROST_SEC = 7 * 3600
COMPRESSOR_LOCKOUT_SEC = 180
LONG_RUN_WARN_SEC = 25200  # 7 h — service guide fault threshold
CLOUD_SYNC_EVERY_POLLS = 300  # ~20 min at 4 s interval
PRUNE_EVERY_POLLS = 720
MAX_REAL_REST_SEC = 5400  # > 90 min is missing telemetry, not compressor rest

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

def classify_cycle(avg_power, duration_sec, cooling_since_sec=0, powers=None, voltage=None, current=None):
    """
    Classify cycle into 'defrost' (heating element) vs 'cooling' (compressor motor).
    Samsung RT34MB No Frost rules:
    1. Defrost duration is strictly within [8 min .. 28 min] (480s .. 1680s).
    2. Defrost requires cumulative compressor cooling before it can trigger (at least 4.5 hours = 16200s).
    3. Physics Check: If real measured current is present:
       - Pure resistive heater: cos(phi) >= 0.92
       - Compressor induction motor: cos(phi) <= 0.85
    """
    p = float(avg_power or 0)
    dur = int(duration_sec or 0)
    acc = int(cooling_since_sec or 0)
    
    # Rule 1: Defrost on this fridge is strictly 8..28 min. Longer is ALWAYS cooling.
    if dur < DEFROST_MIN_SEC or dur > DEFROST_MAX_SEC:
        return "cooling"

    # Rule 2: Defrost cannot trigger repeatedly without cumulative cooling (at least 4.5 h)
    if acc < int(4.5 * 3600):
        return "cooling"

    # Rule 3: Physics Check - Power Factor cos(phi) using REAL measured current
    if voltage and current and float(voltage) > 150 and float(current) > 0.35 and p > 40:
        va = float(voltage) * float(current)
        if va > 0:
            cos_phi = p / va
            if cos_phi <= 0.85:
                return "cooling"
            if cos_phi >= 0.92:
                return "defrost"

    # Rule 4: Voltage-normalized power check: P_nominal = P_actual * (220 / U)^2
    norm_p = p
    if voltage and float(voltage) > 150:
        norm_p = p * ((220.0 / float(voltage)) ** 2)

    in_heater_band = (DEFROST_POWER_MIN <= norm_p <= DEFROST_POWER_MAX) or (DEFROST_POWER_MIN <= p <= DEFROST_POWER_MAX)
    if in_heater_band:
        return "defrost"
    return "cooling"

def cooling_since_last_defrost_sec():
    try:
        conn = sqlite3.connect(DB_FILE)
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
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, duration_sec, avg_power, cycle_type FROM cycles ORDER BY start_time ASC, id ASC"
        )
        rows = cur.fetchall()
        cooling_acc = 0
        for cid, dur, avg_p, old_type in rows:
            new_type = classify_cycle(avg_p, dur, cooling_acc)
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

def food_safety_label(gap_sec):
    if gap_sec <= 14400:
        return "🟢 Оценка без датчика: за ≤4 ч камера обычно теряет около 1°C"
    if gap_sec <= 28800:
        return "🟡 Оценка без датчика: 4–8 ч, морозилка может подняться примерно до −10°C"
    return "🔴 Длительное отключение: проверьте продукты (оценка без датчика)"

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
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT MAX(end_time) FROM cycles")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return None

def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None

def format_gap_str(sec):
    """Journal durations as hours and minutes (no seconds)."""
    if sec is None or sec < 0:
        return None
    total_min = (int(sec) + 30) // 60
    if total_min < 1:
        return "1 мин" if sec > 0 else "0 мин"
    h = total_min // 60
    m = total_min % 60
    if h > 0 and m > 0:
        return f"{h} ч {m} мин"
    if h > 0:
        return f"{h} ч"
    return f"{m} мин"

def fill_rest_after(dt_end, next_st, next_end_label, raw_blackouts, work_dur):
    """Rest that follows a work cycle: stop -> next start (left-to-right in the journal)."""
    empty = (0, "—", "—", "—", "—")
    if not dt_end or not next_st or next_st <= dt_end:
        return empty
    diff_sec = int((next_st - dt_end).total_seconds())
    if diff_sec < 45 and next_end_label != "сейчас":
        return empty
    is_blackout_cross = False
    for b_item in raw_blackouts:
        b1dt = parse_iso(b_item[0])
        b2dt = parse_iso(b_item[1])
        if not b1dt or not b2dt:
            continue
        t_a = dt_end.timestamp()
        t_b = next_st.timestamp()
        b1 = b1dt.timestamp()
        b2 = b2dt.timestamp()
        if (t_a <= b1 and t_b >= b2) or (b1 <= t_b <= b2) or (b1 <= t_a <= b2):
            is_blackout_cross = True
            break
    if is_blackout_cross:
        return (0, "затем отключение", "—", "—", "—")
    if diff_sec > MAX_REAL_REST_SEC:
        return (0, "нет записи", "—", "—", "—")
    rest_start = dt_end.strftime("%H:%M")
    rest_end = next_end_label or next_st.strftime("%H:%M")
    krv = "—"
    if work_dur:
        krv = f"{round(work_dur / (work_dur + diff_sec), 2):.2f}"
    display_gap = format_gap_str(diff_sec) or "0 мин"
    return (diff_sec, display_gap, rest_start, rest_end, krv)

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
        conn = sqlite3.connect(DB_FILE)
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
        conn = sqlite3.connect(DB_FILE)
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
        
        conn = sqlite3.connect(DB_FILE)
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

sync_cloud_history()
reclassify_stored_cycles()

def find_open_cycle_start():
    """First sustained compressor-on run after the last saved cycle end.

    Ignores 1-sample Tuya glitches and idle rows written during a process restart.
    """
    last_end = get_latest_end_time() or "1970-01-01T00:00:00"
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
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
    raise SystemExit(0)

_local_dev = None
_local_ident = None
_local_fail_until = 0.0
_last_ip_scan = 0.0
_last_lan_rule = 0.0

def _ensure_lan_route():
    """Keep 192.168.0.0/24 on eth0; byedpi tun0 otherwise swallows the plug."""
    global _last_lan_rule
    now = time.time()
    if now - _last_lan_rule < 30:
        return
    _last_lan_rule = now
    try:
        subprocess.run(["ip", "rule", "del", "pref", "9000"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        subprocess.run(["ip", "rule", "add", "to", "192.168.0.0/24", "lookup", "main", "pref", "9000"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        subprocess.run(["ip", "rule", "del", "pref", "9001"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        subprocess.run(["ip", "rule", "add", "from", "192.168.0.0/24", "lookup", "main", "pref", "9001"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
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

def _scan_tuya_port(subnet="192.168.0."):
    try:
        from concurrent.futures import ThreadPoolExecutor
    except Exception:
        return []
    found = []

    def check(i):
        ip = subnet + str(i)
        return ip if _tcp_open(ip, 6668, 0.4) else None

    try:
        with ThreadPoolExecutor(max_workers=32) as pool:
            for ip in pool.map(check, range(2, 255)):
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
    hits = _scan_tuya_port()
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

def prune_old_measurements():
    """Prunes raw 4-second telemetry older than 7 days to keep SQLite light on TV Box"""
    try:
        cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("DELETE FROM measurements WHERE timestamp < ?", (cutoff,))
        conn.commit()
        conn.close()
    except Exception:
        pass

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
    update_last_blackout_state()
    
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
            prune_old_measurements()
            
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
                        conn = sqlite3.connect(DB_FILE)
                        cur = conn.cursor()
                        cur.execute('''
                            INSERT OR IGNORE INTO cycles (start_time, end_time, duration_sec, avg_power, avg_voltage, cycle_type)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (state["cycle_start_time"], end_iso, duration, round(avg_p, 1), round(avg_v, 1), c_type))
                        conn.commit()
                        conn.close()
                    
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
                    
                    c_prog = min(1.0, state["cycle_duration_sec"] / 3600.0)
                    if state.get("temp_freezer_estimated", True):
                        state["temp_freezer"] = round(-16.0 - (5.0 * c_prog), 1)
                    if state.get("temp_fridge_estimated", True):
                        state["temp_fridge"] = round(5.2 - (2.2 * c_prog), 1)
                        
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
                    r_prog = min(1.0, state["rest_duration_sec"] / 3000.0)
                    if state.get("temp_freezer_estimated", True):
                        state["temp_freezer"] = round(-19.5 + (3.0 * r_prog), 1)
                    if state.get("temp_fridge_estimated", True):
                        state["temp_fridge"] = round(3.6 + (1.4 * r_prog), 1)
                
                try:
                    conn = sqlite3.connect(DB_FILE)
                    cur = conn.cursor()
                    cur.execute('''
                        INSERT INTO measurements (timestamp, power, voltage, current, is_running, mode, temp_freezer, temp_fridge)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (now_iso, state["power"], state["voltage"], state["current"], 1 if state["is_running"] else 0, state["current_mode"], state["temp_freezer"], state["temp_fridge"]))
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

        if state.get("connected"):
            if state.get("connection_source") == "local_wifi":
                sleep_sec = 3 if state.get("is_running") else 4
            else:
                # Cloud Eco-Fallback: 25s when running, 45s when resting (guarantees safe quota consumption if LAN is down)
                sleep_sec = 25 if state.get("is_running") else 45
        time.sleep(max(2, int(sleep_sec)))

t = threading.Thread(target=tuya_poller, daemon=True)
t.start()

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
    res_state = dict(state)
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
    """Secure Config Endpoint: Masks secrets on GET, restricts POST to localhost"""
    if request.method == "POST":
        if not is_localhost_request():
            return jsonify({"error": "Forbidden: Configuration changes allowed only from localhost"}), 403
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
        save_config(cfg)
        return jsonify({"status": "saved"})
    
    cfg = load_config()
    is_local = is_localhost_request()
    raw_key = cfg.get("api_key", "")
    raw_dev = cfg.get("device_id", "")
    
    safe_key = raw_key if is_local else (raw_key[:4] + "..." + raw_key[-4:] if len(raw_key) > 8 else "***")
    safe_dev = raw_dev if is_local else (raw_dev[:4] + "..." + raw_dev[-4:] if len(raw_dev) > 8 else "***")
    
    safe_cfg = {
        "api_region": cfg.get("api_region", "eu"),
        "api_key": safe_key,
        "api_secret": "********" if cfg.get("api_secret") else "",
        "device_id": safe_dev,
        "temp_sensor_id": cfg.get("temp_sensor_id", "")
    }
    return jsonify(safe_cfg)

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

@app.route("/api/history")
def get_history():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT timestamp, power, voltage, current, is_running, temp_freezer, temp_fridge FROM measurements ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    
    # One row per cycles record. GROUP BY end-minute mixed avg_power/cycle_type
    # from an arbitrary SQLite row when two cycles ended in the same minute.
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
    raw_cycles = cur.fetchall()
    
    # Merge micro-split cycles (where rest between fragments is <= 90 seconds)
    merged_raw_cycles = []
    for c in raw_cycles:
        if not merged_raw_cycles:
            merged_raw_cycles.append(list(c))
            continue
        prev = merged_raw_cycles[-1]
        prev_end = parse_iso(prev[1])
        cur_st = parse_iso(c[0])
        if prev_end and cur_st and 0 <= (cur_st - prev_end).total_seconds() <= 90 and prev[5] == c[5]:
            prev[1] = c[1]
            prev[2] = prev[2] + c[2] + int((cur_st - prev_end).total_seconds())
            prev[3] = round((prev[3] + c[3]) / 2.0, 1)
            prev[4] = round((prev[4] + c[4]) / 2.0, 1)
        else:
            merged_raw_cycles.append(list(c))
    raw_cycles = merged_raw_cycles
    
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
            
            # Also insert directly into main events stream for main table
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

    live_row = build_live_journal_row()
    if live_row:
        enhanced_cycles.insert(0, live_row)

    overall_krv = round(total_work_sec / (total_work_sec + total_rest_sec), 2) if (total_work_sec + total_rest_sec) > 0 else 0.38
    
    return jsonify({
        "measurements": [
            {
                "time": r[0][11:16], 
                "power": r[1], 
                "voltage": r[2], 
                "current": r[3], 
                "is_running": bool(r[4]),
                "temp_freezer": r[5] if len(r)>5 and r[5] is not None else -18.2,
                "temp_fridge": r[6] if len(r)>6 and r[6] is not None else 4.1
            }
            for r in reversed(rows)
        ],
        "cycles": enhanced_cycles,
        "blackouts": formatted_blackouts,
        "summary": {
            "overall_krv": overall_krv,
            "total_cycles": len(enhanced_cycles),
            "krv_status": "🟢 Отличный (энергоэффективный)" if overall_krv <= 0.50 else "🟡 Повышенный"
        }
    })

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
    print(f"SmartFridge Server running at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
