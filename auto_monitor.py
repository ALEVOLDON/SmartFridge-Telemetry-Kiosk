import os
import sys
import time
import json
import sqlite3
import argparse
import threading
import signal
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
    for col in ["mode TEXT", "temp_freezer REAL", "temp_fridge REAL"]:
        try:
            c.execute(f"ALTER TABLE measurements ADD COLUMN {col}")
        except Exception:
            pass
    try:
        c.execute("ALTER TABLE cycles ADD COLUMN cycle_type TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()

init_db()

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

def classify_cycle(avg_power, duration_sec, cooling_since_sec=0, powers=None):
    """Defrost only if heater-like watts, defrost-length, AND enough compressor hours since last defrost."""
    p = float(avg_power or 0)
    dur = int(duration_sec or 0)
    acc = int(cooling_since_sec or 0)
    in_heater_band = DEFROST_POWER_MIN <= p <= DEFROST_POWER_MAX
    in_defrost_window = DEFROST_MIN_SEC <= dur <= DEFROST_MAX_SEC
    enough_cooling = acc >= MIN_COOLING_BEFORE_DEFROST_SEC
    if not (in_heater_band and in_defrost_window and enough_cooling):
        return "cooling"
    if powers and len(powers) >= 6:
        mean = sum(powers) / len(powers)
        if mean > 0:
            var = sum((x - mean) ** 2 for x in powers) / len(powers)
            if (var ** 0.5) / mean > 0.12:
                return "cooling"
    return "defrost"

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
    if diff_sec < 120:
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
    return (diff_sec, format_gap_str(diff_sec) or "—", rest_start, rest_end, krv)

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
    """Fetches historical logs from Tuya Cloud to reconstruct cycles and blackout outages"""
    cfg = load_config()
    if not (cfg.get("api_key") and cfg.get("device_id") and cfg.get("api_secret")):
        return False
    try:
        c = get_cloud_client(cfg)
        now_ts = int(time.time() * 1000)
        start_ts = now_ts - (48 * 3600 * 1000)
        res = c.getdevicelog(
            deviceid=cfg["device_id"].strip(),
            start=start_ts,
            end=now_ts,
            evtype=7,
            size=0,
            max_fetches=40
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
    warning_long_sent = False
    poll_count = 0
    active_streak = 0
    idle_streak = 0
    ON_STREAK = 3
    OFF_STREAK = 3
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
            
        try:
            c = get_cloud_client(cfg)
            res = c.getstatus(cfg["device_id"].strip())
            
            if res and isinstance(res, dict) and "result" in res:
                state["connected"] = True
                state["error_message"] = ""
                
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
                
                power = p_raw / 10.0 if p_raw > 500 else p_raw
                voltage = v_raw / 10.0 if v_raw > 500 else v_raw
                current = i_raw / 1000.0 if i_raw > 100 else i_raw
                
                state["power"] = round(power, 1)
                state["voltage"] = round(voltage, 1)
                state["current"] = round(current, 2)
                state["last_update"] = datetime.now().strftime("%H:%M:%S")
                
                # Optional Tuya probe is treated as freezer-only; fridge stays a model.
                temp_sensor_id = cfg.get("temp_sensor_id", "").strip()
                state["temp_sensor_id"] = temp_sensor_id
                if temp_sensor_id and poll_count % 5 == 0:
                    try:
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
                        c_type = classify_cycle(avg_p, duration, cooling_since_last_defrost_sec(), cycle_powers)
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
                    cycle_powers = []
                    cycle_voltages = []
                
                if state["is_running"]:
                    state["rest_duration_sec"] = 0
                    if not state["cycle_start_time"]:
                        actual_start = find_open_cycle_start()
                        state["cycle_start_time"] = actual_start if actual_start else now_iso
                    start_dt = parse_iso(state["cycle_start_time"]) or datetime.now()
                    state["cycle_duration_sec"] = int((datetime.now() - start_dt).total_seconds())
                    cycle_powers.append(power)
                    cycle_voltages.append(voltage)
                    update_restart_lockout(True)
                    
                    avg_recent_p = sum(cycle_powers[-8:]) / len(cycle_powers[-8:]) if cycle_powers else power
                    if classify_cycle(avg_recent_p, state["cycle_duration_sec"], cooling_since_last_defrost_sec(), cycle_powers) == "defrost":
                        state["current_mode"] = "defrost"
                        state["mode_title"] = "🔥 АВТООТТАЙКА NO FROST (ТЭН 170W)"
                    else:
                        state["current_mode"] = "cooling"
                        state["mode_title"] = "🟢 КОМПРЕССОР РАБОТАЕТ (ОХЛАЖДЕНИЕ)"
                    
                    progress = min(1.0, state["cycle_duration_sec"] / 1800.0)
                    if state.get("temp_freezer_estimated", True):
                        state["temp_freezer"] = round(-16.0 - (3.5 * progress), 1)
                    if state.get("temp_fridge_estimated", True):
                        state["temp_fridge"] = round(5.2 - (1.6 * progress), 1)
                    
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
                state["error_message"] = res.get("msg", "Tuya Cloud request error")
                
        except Exception as e:
            state["connected"] = False
            state["error_message"] = str(e)
            
        time.sleep(4)

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
    return jsonify(state)

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
    
    # Cycles - group by exact date and start time to keep all history intact!
    cur.execute("""
        SELECT 
            MIN(start_time) as full_start,
            MAX(end_time) as full_end,
            MAX(duration_sec) as max_dur, 
            avg_power, 
            avg_voltage, 
            cycle_type 
        FROM cycles 
        WHERE duration_sec >= 120
        GROUP BY DATE(end_time), strftime('%H:%M', end_time)
        ORDER BY MIN(start_time) ASC
    """)
    raw_cycles = cur.fetchall()
    
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
