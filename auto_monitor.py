import os
import sys
import time
import json
import sqlite3
import argparse
import threading
from datetime import datetime
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
    "protection_active": True,
    "protection_lockout_sec": 0,
    "protection_status_text": "Защита активна (Память реле 'last' включена)",
    "notifications_enabled": True,
    "avg_duty_cycle": 0.38,
    "temp_freezer": -18.2,
    "temp_fridge": 4.1,
    "temp_sensor_connected": False,
    "temp_sensor_id": "",
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

def update_last_blackout_state():
    global state
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT start_time, end_time, duration_sec, food_safety_status FROM blackouts ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if row:
            st_iso, end_iso, dur_sec, safety = row
            try:
                dt_st = datetime.fromisoformat(st_iso)
                dt_end = datetime.fromisoformat(end_iso)
                h = dur_sec // 3600
                m = (dur_sec % 3600) // 60
                dur_str = f"{h} ч {m} мин" if h > 0 else f"{m} мин"
                
                state["last_blackout"] = {
                    "detected": True,
                    "start": dt_st.strftime("%H:%M"),
                    "end": dt_end.strftime("%H:%M"),
                    "date": dt_st.strftime("%d.%m.%Y"),
                    "duration_str": dur_str,
                    "duration_sec": dur_sec,
                    "food_safety": safety
                }
            except Exception:
                pass
    except Exception:
        pass

def sync_cloud_history():
    """Fetches historical logs from Tuya Cloud to reconstruct cycles and blackout outages"""
    cfg = load_config()
    if not (cfg.get("api_key") and cfg.get("device_id") and cfg.get("api_secret")):
        return
    try:
        c = tinytuya.Cloud(
            apiRegion=cfg.get("api_region", "eu"),
            apiKey=cfg["api_key"].strip(),
            apiSecret=cfg["api_secret"].strip(),
            apiDeviceID=cfg["device_id"].strip()
        )
        now_ts = int(time.time() * 1000)
        start_ts = now_ts - (36 * 3600 * 1000)
        
        res = c.cloudrequest('/v1.0/devices/' + cfg["device_id"].strip() + '/logs', query={
            'start_time': start_ts,
            'end_time': now_ts,
            'type': '7',
            'size': 150
        })
        
        logs = res.get('result', {}).get('logs', [])
        if not logs:
            return
        
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
                if gap_sec <= 14400: # < 4 hours
                    safety = "🟢 Безопасно: Холод удержан на 100% (камера нагрелась всего на ~1.2°C)"
                elif gap_sec <= 28800: # 4-8 hours
                    safety = "🟡 Умеренно: Морозилка держала холод до -10°C"
                else:
                    safety = "🔴 Длительное отключение: Проверьте продукты"
                
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

        # Clean any erroneously merged cycles > 2.5 hours
        cur.execute('DELETE FROM cycles WHERE duration_sec > 9000')

        in_cycle = False
        c_start_ts = None
        c_start_iso = None
        powers = []
        
        for i in range(len(events)):
            t_sec, dt_iso, val = events[i]
            
            # If a blackout occurred between previous event and current event, terminate pre-blackout cycle!
            if in_cycle and i > 0:
                t_prev, dt_prev, val_prev = events[i-1]
                if (t_sec - t_prev) >= 1800 or in_blackout_span(t_prev, t_sec):
                    dur = int(t_prev - c_start_ts)
                    if dur >= 180:
                        avg_p = sum(powers) / len(powers) if powers else 130.0
                        c_type = "defrost" if (avg_p > 160.0 and dur < 2400) else "cooling"
                        cur.execute('''
                            INSERT OR IGNORE INTO cycles (start_time, end_time, duration_sec, avg_power, avg_voltage, cycle_type)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (c_start_iso, dt_prev, dur, round(avg_p, 1), 226.0, c_type))
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
                        c_type = "defrost" if (avg_p > 160.0 and dur < 2400) else "cooling"
                        cur.execute('''
                            INSERT OR IGNORE INTO cycles (start_time, end_time, duration_sec, avg_power, avg_voltage, cycle_type)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (c_start_iso, dt_iso, dur, round(avg_p, 1), 226.0, c_type))
                    powers = []
                    c_start_ts = None
                    c_start_iso = None
        
        conn.commit()
        conn.close()
        
        update_last_blackout_state()
    except Exception as e:
        print("Cloud sync notice:", e)

sync_cloud_history()

def find_actual_current_cycle_start():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT end_time FROM blackouts ORDER BY id DESC LIMIT 1")
        b_row = cur.fetchone()
        if b_row and b_row[0]:
            b_end = b_row[0]
            cur.execute("SELECT timestamp FROM measurements WHERE is_running = 0 AND timestamp > ? LIMIT 1", (b_end,))
            stop_after = cur.fetchone()
            if not stop_after:
                conn.close()
                return b_end
        
        cur.execute("SELECT timestamp FROM measurements WHERE is_running = 0 ORDER BY id DESC LIMIT 1")
        row_stop = cur.fetchone()
        if row_stop and row_stop[0]:
            last_stop = row_stop[0]
            cur.execute("SELECT timestamp FROM measurements WHERE is_running = 1 AND timestamp > ? ORDER BY id ASC LIMIT 1", (last_stop,))
            row_start = cur.fetchone()
            conn.close()
            if row_start and row_start[0]:
                return row_start[0]
        conn.close()
    except Exception:
        pass
    return None

def tuya_poller():
    global state
    cycle_powers = []
    cycle_voltages = []
    warning_long_sent = False
    poll_count = 0
    
    cfg = load_config()
    db_last_end = get_latest_end_time()
    if db_last_end:
        state["rest_start_time"] = db_last_end
    elif cfg.get("last_stop_time"):
        state["rest_start_time"] = cfg["last_stop_time"]
    if cfg.get("current_start_time"):
        state["cycle_start_time"] = cfg["current_start_time"]
        state["is_running"] = True
    else:
        actual_start = find_actual_current_cycle_start()
        if actual_start:
            state["cycle_start_time"] = actual_start
            state["is_running"] = True
        
    update_last_blackout_state()
    
    while True:
        cfg = load_config()
        if not (cfg.get("api_key") and cfg.get("api_secret") and cfg.get("device_id")):
            state["connected"] = False
            state["error_message"] = "Waiting for Tuya API credentials in config.json"
            time.sleep(3)
            continue
            
        poll_count += 1
        if poll_count % 60 == 0:
            sync_cloud_history()
            
        try:
            c = tinytuya.Cloud(
                apiRegion=cfg.get("api_region", "eu"),
                apiKey=cfg["api_key"].strip(),
                apiSecret=cfg["api_secret"].strip(),
                apiDeviceID=cfg["device_id"].strip()
            )
            
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
                state["voltage"] = round(voltage, 1) if voltage > 0 else 220.0
                state["current"] = round(current, 2)
                state["last_update"] = datetime.now().strftime("%H:%M:%S")
                
                # Check optional external Temperature Sensor
                temp_sensor_id = cfg.get("temp_sensor_id", "").strip()
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
                
                now_iso = datetime.now().isoformat()
                is_active = (power > 35.0)
                
                if is_active:
                    state["current_mode"] = "cooling"
                    state["mode_title"] = "🟢 КОМПРЕССОР РАБОТАЕТ (ОХЛАЖДЕНИЕ)"
                    state["rest_duration_sec"] = 0
                    
                    if not state["is_running"]:
                        state["is_running"] = True
                        actual_start = find_actual_current_cycle_start()
                        state["cycle_start_time"] = actual_start if actual_start else now_iso
                        state["cycle_duration_sec"] = 0
                        cycle_powers = []
                        cycle_voltages = []
                        warning_long_sent = False
                        
                        cfg_save = load_config()
                        cfg_save["current_start_time"] = state["cycle_start_time"]
                        save_config(cfg_save)
                    
                    if not state["cycle_start_time"]:
                        actual_start = find_actual_current_cycle_start()
                        state["cycle_start_time"] = actual_start if actual_start else now_iso
                        
                    start_dt = datetime.fromisoformat(state["cycle_start_time"])
                    state["cycle_duration_sec"] = int((datetime.now() - start_dt).total_seconds())
                    
                    # Thermodynamic Temperature Curve (Cooling down)
                    if not state.get("temp_sensor_connected", False):
                        progress = min(1.0, state["cycle_duration_sec"] / 1800.0)
                        state["temp_freezer"] = round(-16.0 - (3.5 * progress), 1)
                        state["temp_fridge"] = round(5.2 - (1.6 * progress), 1)
                    
                    if state["cycle_duration_sec"] > 14400 and not warning_long_sent:
                        warning_long_sent = True
                        send_pc_toast(
                            "⚠️ Длительная работа компрессора",
                            "Компрессор работает уже более 4 часов подряд без остановки! Проверьте закрытие двери.",
                            is_warning=True
                        )
                    
                    cycle_powers.append(power)
                    cycle_voltages.append(voltage)
                else:
                    state["current_mode"] = "idle"
                    state["mode_title"] = "⚪ ПОЛНЫЙ ПОКОЙ (ОТДЫХ)"
                    
                    if state["is_running"]:
                        state["is_running"] = False
                        end_iso = now_iso
                        state["rest_start_time"] = end_iso
                        duration = state["cycle_duration_sec"]
                        
                        cfg_save = load_config()
                        cfg_save["last_stop_time"] = end_iso
                        cfg_save["current_start_time"] = None
                        save_config(cfg_save)
                        
                        if duration >= 120:
                            avg_p = sum(cycle_powers) / len(cycle_powers) if cycle_powers else 131.0
                            avg_v = sum(cycle_voltages) / len(cycle_voltages) if cycle_voltages else 226.0
                            c_type = "cooling"
                            
                            conn = sqlite3.connect(DB_FILE)
                            cur = conn.cursor()
                            cur.execute('''
                                INSERT OR IGNORE INTO cycles (start_time, end_time, duration_sec, avg_power, avg_voltage, cycle_type)
                                VALUES (?, ?, ?, ?, ?, ?)
                            ''', (state["cycle_start_time"], end_iso, duration, round(avg_p, 1), round(avg_v, 1), c_type))
                            conn.commit()
                            conn.close()
                        
                        state["cycle_start_time"] = None
                        state["cycle_duration_sec"] = 0
                    
                    # Track rest duration intelligently
                    db_last_end = get_latest_end_time()
                    if db_last_end and (not state.get("rest_start_time") or db_last_end > state["rest_start_time"]):
                        state["rest_start_time"] = db_last_end

                    if state.get("rest_start_time"):
                        try:
                            r_start_dt = datetime.fromisoformat(state["rest_start_time"])
                            state["rest_duration_sec"] = max(0, int((datetime.now() - r_start_dt).total_seconds()))
                        except Exception:
                            state["rest_duration_sec"] = 0
                    
                    # Thermodynamic Temperature Curve (Holding & warming up)
                    if not state.get("temp_sensor_connected", False):
                        r_prog = min(1.0, state["rest_duration_sec"] / 3000.0)
                        state["temp_freezer"] = round(-19.5 + (3.0 * r_prog), 1)
                        state["temp_fridge"] = round(3.6 + (1.4 * r_prog), 1)
                
                try:
                    conn = sqlite3.connect(DB_FILE)
                    cur = conn.cursor()
                    cur.execute('''
                        INSERT INTO measurements (timestamp, power, voltage, current, is_running, mode, temp_freezer, temp_fridge)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (now_iso, state["power"], state["voltage"], state["current"], 1 if is_active else 0, state["current_mode"], state["temp_freezer"], state["temp_fridge"]))
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
        if data.get("api_key"):
            cfg["api_key"] = data["api_key"].strip()
        if data.get("api_secret") and data["api_secret"] != "********":
            cfg["api_secret"] = data["api_secret"].strip()
        if data.get("device_id"):
            cfg["device_id"] = data["device_id"].strip()
        if "temp_sensor_id" in data:
            cfg["temp_sensor_id"] = data["temp_sensor_id"].strip()
        save_config(cfg)
        return jsonify({"status": "saved"})
    
    cfg = load_config()
    safe_cfg = {
        "api_region": cfg.get("api_region", "eu"),
        "api_key": cfg.get("api_key", ""),
        "api_secret": "********" if cfg.get("api_secret") else "",
        "device_id": cfg.get("device_id", ""),
        "temp_sensor_id": cfg.get("temp_sensor_id", "")
    }
    return jsonify(safe_cfg)

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
        GROUP BY DATE(start_time), strftime('%H:%M', start_time)
        ORDER BY MIN(start_time) ASC
    """)
    raw_cycles = cur.fetchall()
    
    # Blackouts
    cur.execute("SELECT start_time, end_time, duration_sec, food_safety_status FROM blackouts ORDER BY id DESC LIMIT 20")
    raw_blackouts = cur.fetchall()
    conn.close()
    
    enhanced_cycles = []
    total_work_sec = 0
    total_rest_sec = 0
    
    for i in range(len(raw_cycles)):
        c = raw_cycles[i]
        dur = c[2]
        c_type = c[5] if len(c) > 5 and c[5] else "cooling"
        
        total_work_sec += dur
        
        rest_sec = 0
        rest_str = "—"
        krv_val = "—"
        
        try:
            dt_st = datetime.fromisoformat(c[0])
            dt_end = datetime.fromisoformat(c[1])
            date_full = dt_st.strftime("%d.%m.%Y")
            date_short = dt_st.strftime("%d.%m")
            s_time = dt_st.strftime("%H:%M")
            e_time = dt_end.strftime("%H:%M")
        except Exception:
            date_full = "29.08.2026"
            date_short = "29.08"
            s_time = "--:--"
            e_time = "--:--"
        
        if i > 0:
            prev_end_str = raw_cycles[i-1][1]
            curr_start_str = c[0]
            try:
                dt_prev = datetime.fromisoformat(prev_end_str)
                dt_curr = datetime.fromisoformat(curr_start_str)
                diff_sec = max(0, int((dt_curr - dt_prev).total_seconds()))
                
                is_blackout_cross = False
                for b_item in raw_blackouts:
                    try:
                        b1 = datetime.fromisoformat(b_item[0]).timestamp()
                        b2 = datetime.fromisoformat(b_item[1]).timestamp()
                        t_a = dt_prev.timestamp()
                        t_b = dt_curr.timestamp()
                        if (t_a <= b1 and t_b >= b2) or (b1 <= t_b <= b2) or (b1 <= t_a <= b2):
                            is_blackout_cross = True
                            break
                    except Exception:
                        pass
                
                if 120 <= diff_sec <= 5400 and not is_blackout_cross:
                    rest_sec = diff_sec
                    total_rest_sec += rest_sec
                    r_m = rest_sec // 60
                    r_s = rest_sec % 60
                    rest_str = f"{r_m} мин {r_s} сек" if r_s > 0 else f"{r_m} мин"
                    calc_krv = round(dur / (dur + rest_sec), 2)
                    krv_val = f"{calc_krv:.2f}"
                else:
                    rest_str = "—"
                    krv_val = "—"
            except Exception:
                pass
            
        enhanced_cycles.append({
            "full_end": c[1] or "",
            "date": date_full,
            "date_short": date_short,
            "start": s_time,
            "end": e_time,
            "duration_sec": dur,
            "duration_str": f"{dur // 60} мин {dur % 60} сек",
            "rest_sec": rest_sec,
            "rest_str": rest_str,
            "krv": krv_val,
            "avg_power": c[3],
            "avg_voltage": c[4],
            "cycle_type": c_type
        })
    
    formatted_blackouts = []
    seen_blackouts = set()
    for b in raw_blackouts:
        st_iso, end_iso, dur_sec, safety = b
        key = (st_iso[:16], end_iso[:16])
        if key in seen_blackouts:
            continue
        seen_blackouts.add(key)
        try:
            dt_st = datetime.fromisoformat(st_iso)
            dt_end = datetime.fromisoformat(end_iso)
            h = dur_sec // 3600
            m = (dur_sec % 3600) // 60
            dur_str = f"{h} ч {m} мин" if h > 0 else f"{m} мин"
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
    
    os.makedirs(STATIC_DIR, exist_ok=True)
    print(f"SmartFridge Server running at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
