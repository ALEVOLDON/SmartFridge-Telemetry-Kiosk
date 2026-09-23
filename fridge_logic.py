"""Pure helpers for RT34MB telemetry. No Flask, TinyTuya, or SQLite."""
import ipaddress
from datetime import datetime

DEFROST_POWER_MIN = 149.0
DEFROST_POWER_MAX = 188.0
DEFROST_MIN_SEC = 480
DEFROST_MAX_SEC = 1860
MIN_COOLING_BEFORE_DEFROST_SEC = int(5.0 * 3600)
MAX_REAL_REST_SEC = 5400
DEFAULT_LAN_SUBNET = "192.168.0.0/24"


def classify_cycle(avg_power, duration_sec, cooling_since_sec=0, powers=None, voltage=None, current=None):
    """
    Classify cycle into 'defrost' (heating element) vs 'cooling' (compressor motor).
    Samsung RT34MB No Frost rules:
    1. Defrost duration is strictly within [8 min .. 31 min] (480s .. 1860s). Longer is ALWAYS cooling.
    2. Defrost requires cumulative compressor cooling before it can trigger (at least 5.0 hours = 18000s).
    3. Power curve stability: a pure heating element (ТЭН) has nearly flat power (core delta <= 16W),
       while a compressor has start-inrush and pressure drops (delta >= 25W).
    4. Power range: Heater operates at 149W .. 188W (nominal ~160W at 220V, down to ~150W at 214V).
    """
    p = float(avg_power or 0)
    dur = int(duration_sec or 0)
    acc = int(cooling_since_sec or 0)

    if dur < DEFROST_MIN_SEC or dur > DEFROST_MAX_SEC:
        return "cooling"

    if acc < MIN_COOLING_BEFORE_DEFROST_SEC:
        return "cooling"

    if powers and len(powers) >= 6:
        active = [x for x in powers if x > 50.0]
        if len(active) >= 4:
            core = active[2:-2] if len(active) > 6 else active
            delta = max(core) - min(core)
            if delta > 16.0:
                return "cooling"

    if DEFROST_POWER_MIN <= p <= DEFROST_POWER_MAX:
        return "defrost"

    return "cooling"


def food_safety_label(gap_sec):
    if gap_sec <= 14400:
        return "🟢 Оценка без датчика: за ≤4 ч камера обычно теряет около 1°C"
    if gap_sec <= 28800:
        return "🟡 Оценка без датчика: 4–8 ч, морозилка может подняться примерно до −10°C"
    return "🔴 Длительное отключение: проверьте продукты (оценка без датчика)"


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


def fill_rest_after(dt_end, next_st, next_end_label, raw_blackouts, work_dur, max_real_rest_sec=MAX_REAL_REST_SEC):
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
    if diff_sec > max_real_rest_sec:
        return (0, "нет записи", "—", "—", "—")
    rest_start = dt_end.strftime("%H:%M")
    rest_end = next_end_label or next_st.strftime("%H:%M")
    krv = "—"
    if work_dur:
        krv = f"{round(work_dur / (work_dur + diff_sec), 2):.2f}"
    display_gap = format_gap_str(diff_sec) or "0 мин"
    return (diff_sec, display_gap, rest_start, rest_end, krv)


def merge_micro_cycles(raw_cycles, max_gap_sec=90):
    """Merge split fragments or overlapping records of the same cycle type."""
    merged = []
    for c in raw_cycles:
        row = list(c)
        if not merged:
            merged.append(row)
            continue
        prev = merged[-1]
        prev_end = parse_iso(prev[1])
        cur_st = parse_iso(row[0])
        cur_end = parse_iso(row[1])
        same_type = (prev[5] if len(prev) > 5 else None) == (row[5] if len(row) > 5 else None)
        if prev_end and cur_st and same_type:
            gap_sec = (cur_st - prev_end).total_seconds()
            if 0 <= gap_sec <= max_gap_sec:
                prev[1] = row[1]
                prev[2] = prev[2] + row[2] + int(gap_sec)
                prev[3] = round((prev[3] + row[3]) / 2.0, 1)
                prev[4] = round((prev[4] + row[4]) / 2.0, 1)
                continue
            elif gap_sec < 0:
                if cur_end and cur_end > prev_end:
                    extension_sec = int((cur_end - prev_end).total_seconds())
                    prev[1] = row[1]
                    prev[2] = prev[2] + extension_sec
                    prev[3] = round((prev[3] + row[3]) / 2.0, 1)
                    prev[4] = round((prev[4] + row[4]) / 2.0, 1)
                continue
        merged.append(row)
    return merged


def duty_cycle(work_sec, rest_sec):
    total = float(work_sec or 0) + float(rest_sec or 0)
    if total <= 0:
        return None
    return round(float(work_sec or 0) / total, 2)


def estimated_chamber_temps(is_running, duration_sec, blackout_sec=0):
    """
    Thermal inertia model used when no physical probe is connected.
    If blackout_sec >= 1800, accounts for thermal leakage during the power outage.
    """
    dur = max(0, int(duration_sec or 0))
    b_sec = max(0, int(blackout_sec or 0))

    if b_sec >= 1800:
        # Thermal leakage during/after power outage
        # Samsung RT34MB polyurethane insulation: warms ~1.4°C/hr in freezer, ~0.8°C/hr in fridge
        hours_off = b_sec / 3600.0
        fz_warmup = min(22.0, hours_off * 1.4)
        fr_warmup = min(12.0, hours_off * 0.8)

        base_fz = -19.5 + fz_warmup
        base_fr = 3.6 + fr_warmup

        if is_running:
            # Compressor pull-down curve: cools down ~4-6°C per hour of run
            c_prog = min(1.0, dur / 7200.0)
            target_fz = -21.0
            target_fr = 3.0
            cur_fz = base_fz - (base_fz - target_fz) * c_prog
            cur_fr = base_fr - (base_fr - target_fr) * c_prog
            return (round(cur_fz, 1), round(cur_fr, 1))
        else:
            # Still off / resting after outage
            r_prog = min(1.0, dur / 3600.0)
            cur_fz = min(20.0, base_fz + (1.5 * r_prog))
            cur_fr = min(20.0, base_fr + (1.0 * r_prog))
            return (round(cur_fz, 1), round(cur_fr, 1))

    # Standard nominal cycle when fridge is in thermal equilibrium
    if is_running:
        c_prog = min(1.0, dur / 3600.0)
        return (round(-16.0 - (5.0 * c_prog), 1), round(5.2 - (2.2 * c_prog), 1))
    r_prog = min(1.0, dur / 3000.0)
    return (round(-19.5 + (3.0 * r_prog), 1), round(3.6 + (1.4 * r_prog), 1))


def _clean_ip(addr):
    if not addr:
        return ""
    raw = str(addr).strip().lower()
    if raw.startswith("::ffff:"):
        raw = raw[7:]
    if "%" in raw:
        raw = raw.split("%", 1)[0]
    return raw


def is_loopback_ip(addr):
    raw = _clean_ip(addr)
    if raw in ("127.0.0.1", "localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False


def is_private_ip(addr):
    raw = _clean_ip(addr)
    if not raw:
        return False
    if is_loopback_ip(raw):
        return True
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    # Tailscale / CGNAT shared space is not always ip.is_private
    return isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10")


def parse_lan_network(cidr, default=DEFAULT_LAN_SUBNET):
    try:
        return ipaddress.ip_network(str(cidr or default).strip(), strict=False)
    except Exception:
        return ipaddress.ip_network(default, strict=False)


def tuya_scan_hosts(net):
    """Host IPs to probe for TCP/6668. Skip huge networks (would scan tens of thousands)."""
    if net is None or net.num_addresses > 256:
        return []
    return [str(h) for h in net.hosts()]
