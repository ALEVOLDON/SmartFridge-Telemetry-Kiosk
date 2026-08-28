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

# Global Telemetry State (No hardcoded personal dates)
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
    "avg_duty_cycle": 0.38
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
            mode TEXT
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
    try:
        c.execute("ALTER TABLE measurements ADD COLUMN mode TEXT")
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
        "last_stop_time": None,
        "current_start_time": None
    }

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)

def sync_cloud_history():
    """Fetches historical logs from Tuya Cloud to reconstruct cycles"""
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
        start_ts = now_ts - (24 * 3600 * 1000)
        
        res = c.cloudrequest('/v1.0/devices/' + cfg["device_id"].strip() + '/logs', query={
            'start_time': start_ts,
            'end_time': now_ts,
            'type': '7',
            'size': 100
        })
        
        logs = res.get('result', {}).get('logs', [])
        if not logs:
            return
        
        events = []
        for row in reversed(logs):
            if row.get('code') == 'cur_power':
                raw_val = float(row.get('value', 0))
                val = raw_val / 10.0 if raw_val > 500 else raw_val
                t_sec = row['event_time'] / 1000.0
                dt_iso = datetime.fromtimestamp(t_sec).isoformat()
                events.append((t_sec, dt_iso, val))
        
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        
        in_cycle = False
        c_start_ts = None
        c_start_iso = None
        powers = []
        
        for t_sec, dt_iso, val in events:
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
        
        conn.commit()
        conn.close()
    except Exception as e:
        print("Cloud sync notice:", e)

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
                
                now_iso = datetime.now().isoformat()
                is_active = (power > 35.0)
                
                if is_active:
                    state["current_mode"] = "cooling"
                    state["mode_title"] = "🟢 КОМПРЕССОР РАБОТАЕТ (ОХЛАЖДЕНИЕ)"
                    state["rest_duration_sec"] = 0
                    
                    if not state["is_running"]:
                        state["is_running"] = True
                        if not state["cycle_start_time"]:
                            state["cycle_start_time"] = now_iso
                        state["cycle_duration_sec"] = 0
                        cycle_powers = []
                        cycle_voltages = []
                        warning_long_sent = False
                        
                        cfg_save = load_config()
                        cfg_save["current_start_time"] = state["cycle_start_time"]
                        save_config(cfg_save)
                    
                    if not state["cycle_start_time"]:
                        state["cycle_start_time"] = now_iso
                        
                    start_dt = datetime.fromisoformat(state["cycle_start_time"])
                    state["cycle_duration_sec"] = int((datetime.now() - start_dt).total_seconds())
                    
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
                    
                    # Track rest duration intelligently from latest cycle end
                    db_last_end = get_latest_end_time()
                    if db_last_end and (not state.get("rest_start_time") or db_last_end > state["rest_start_time"]):
                        state["rest_start_time"] = db_last_end

                    if state.get("rest_start_time"):
                        try:
                            r_start_dt = datetime.fromisoformat(state["rest_start_time"])
                            state["rest_duration_sec"] = max(0, int((datetime.now() - r_start_dt).total_seconds()))
                        except Exception:
                            state["rest_duration_sec"] = 0
                
                try:
                    conn = sqlite3.connect(DB_FILE)
                    cur = conn.cursor()
                    cur.execute('''
                        INSERT INTO measurements (timestamp, power, voltage, current, is_running, mode)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (now_iso, state["power"], state["voltage"], state["current"], 1 if is_active else 0, state["current_mode"]))
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
        save_config(cfg)
        return jsonify({"status": "saved"})
    
    cfg = load_config()
    # Mask sensitive secret before returning over network
    safe_cfg = {
        "api_region": cfg.get("api_region", "eu"),
        "api_key": cfg.get("api_key", ""),
        "api_secret": "********" if cfg.get("api_secret") else "",
        "device_id": cfg.get("device_id", "")
    }
    return jsonify(safe_cfg)

@app.route("/api/history")
def get_history():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT timestamp, power, voltage, current, is_running FROM measurements ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    
    cur.execute("""
        SELECT 
            MIN(start_time) as full_start,
            MAX(end_time) as full_end,
            strftime('%H:%M', MIN(start_time)) as s_time, 
            strftime('%H:%M', MAX(end_time)) as e_time, 
            MAX(duration_sec) as max_dur, 
            avg_power, 
            avg_voltage, 
            cycle_type 
        FROM cycles 
        WHERE duration_sec >= 120
        GROUP BY strftime('%H:%M', end_time)
        ORDER BY MAX(end_time) ASC
    """)
    raw_cycles = cur.fetchall()
    conn.close()
    
    enhanced_cycles = []
    total_work_sec = 0
    total_rest_sec = 0
    
    for i in range(len(raw_cycles)):
        c = raw_cycles[i]
        dur = c[4]
        c_type = c[7] if len(c) > 7 and c[7] else "cooling"
        
        total_work_sec += dur
        
        rest_sec = 0
        rest_str = "— (первый замер)"
        
        if i > 0:
            prev_end_str = raw_cycles[i-1][1]
            curr_start_str = c[0]
            try:
                dt_prev = datetime.fromisoformat(prev_end_str)
                dt_curr = datetime.fromisoformat(curr_start_str)
                rest_sec = max(0, int((dt_curr - dt_prev).total_seconds()))
                total_rest_sec += rest_sec
                r_m = rest_sec // 60
                r_s = rest_sec % 60
                rest_str = f"{r_m} мин {r_s} сек" if r_s > 0 else f"{r_m} мин"
            except Exception:
                pass
        
        krv_val = round(dur / (dur + rest_sec), 2) if (dur + rest_sec) > 0 else 0.40
        if krv_val > 1.0 or krv_val < 0.1:
            krv_val = 0.38
            
        enhanced_cycles.append({
            "start": c[2],
            "end": c[3],
            "duration_sec": dur,
            "rest_sec": rest_sec,
            "rest_str": rest_str,
            "krv": krv_val,
            "avg_power": c[5],
            "avg_voltage": c[6],
            "cycle_type": c_type
        })
    
    overall_krv = round(total_work_sec / (total_work_sec + total_rest_sec), 2) if (total_work_sec + total_rest_sec) > 0 else 0.38
    
    return jsonify({
        "measurements": [
            {"time": r[0][11:16], "power": r[1], "voltage": r[2], "current": r[3], "is_running": bool(r[4])}
            for r in reversed(rows)
        ],
        "cycles": list(reversed(enhanced_cycles)),
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
