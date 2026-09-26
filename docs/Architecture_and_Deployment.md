# 🏗️ Architecture, Hardware & Deployment Guide

This guide details the complete hardware ecosystem, operating system environment, autonomous supervisor setup, and deployment workflows for **SmartFridge-Telemetry-Kiosk**.

---

## 🖥️ 1. Hardware Architecture

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        PHYSICAL IOT ECOSYSTEM                          │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│   🧊 Samsung RT34MB Refrigerator (CoolTech Dynamic No-Frost)           │
│      ├── Compressor: Samsung SK170K-T1U TS1 (200-220V, R-134a 160g)   │
│      └── Defrost Heater: 155-165 W Sheath Heater (cos φ ≈ 1.0)        │
│          ▲                                                             │
│          │ (Power cord)                                                │
│          ▼                                                             │
│   🔌 Tuya 16A Smart Energy Plug (RMS Power Metering Chip BL0937)       │
│      └── Direct Local TCP/UDP Socket (Port 6668, TinyTuya, 0 Cloud)    │
│          ▲                                                             │
│          │ (Wi-Fi 2.4 GHz)                                             │
│          ▼                                                             │
│   🤖 Android TV Box: H96 Max (Host: 192.168.0.103:8088)               │
│      ├── SoC: Rockchip RK3318 (Quad-Core 64-bit ARM Cortex-A53)        │
│      ├── Power: ~3 Watts continuous, 24/7/365 fanless operation        │
│      ├── Network: Dedicated 100 Mbps Ethernet (eth0) + Policy Routing  │
│      ├── OS: Android Linux Kernel + Termux Environment (Python 3.12)   │
│      └── Supervisor: Termux:Boot daemon loop with auto-restart         │
│          ▲                                                             │
│          │ (HTTP LAN :8088)                                            │
│          ▼                                                             │
│   🍏 Door Kiosk: Apple iPad 3 Retina (iOS 9.3.6, MD369KS/A)            │
│      └── Pure ES5 / CSS3 WebKit Interface (/ipad)                      │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 🤖 2. H96 Max Microserver (Termux Linux)

### Why an Android TV Box instead of a Raspberry Pi?
1. **Cost:** An H96 Max TV Box costs under \$15–20 compared to \$80+ for a Raspberry Pi 4 kit.
2. **Power Efficiency:** Draws only **~3 Watts**, negligible electricity impact.
3. **Integrated Hardware:** Built-in eMMC flash storage (resilient to corruption), 100 Mbps RJ45 Ethernet, Wi-Fi, and 64-bit ARM CPU.

### Autonomous Termux:Boot Supervisor
The server automatically launches on boot via `~/.termux/boot/start_fridge.sh`:
```bash
#!/data/data/com.termux/files/usr/bin/sh
export PREFIX=/data/data/com.termux/files/usr
export HOME=/data/data/com.termux/files/home
export PATH=$PREFIX/bin:$PATH
DIR=$HOME/Samsung_RT34MB_Monitor
PY=$PREFIX/bin/python3

termux-wake-lock >/dev/null 2>&1

# Apply static Ethernet & policy routing shield
su 0 sh "$DIR/scripts/h96_keep_eth_ip.sh" >/dev/null 2>&1

cd "$DIR" || exit 0

# Supervisor watchdog loop
(
    while true; do
        su 0 sh "$DIR/scripts/h96_keep_eth_ip.sh" >/dev/null 2>&1
        if ! pgrep -f "auto_monitor.py" >/dev/null 2>&1; then
            "$PY" auto_monitor.py --host 0.0.0.0 --port 8088 >> "$DIR/monitor.log" 2>&1
        fi
        sleep 10
    done
) >/dev/null 2>&1 &
```

---

## 🛡️ 3. Linux Policy Routing Shield (VPN Isolation)

When VPN clients (e.g. WireGuard, AWG, OpenVPN) run on the TV Box, standard default routes redirect all traffic through the `tun0` interface, breaking local LAN access to the smart plug and web dashboard.

To guarantee uninterrupted LAN telemetry, Linux Policy Routing rules are enforced:
```bash
# Pin local subnet to physical eth0 interface regardless of default gateway
ip rule add to 192.168.0.0/24 table eth0 pref 9000
ip rule add from 192.168.0.103 table eth0 pref 9001
```

---

## 🍏 4. Vintage iPad 3 Kiosk Setup (`/ipad`)

### Hardware Profile:
- **Device:** Apple iPad 3 (3rd Generation, Retina Display, 2012)
- **Model:** `MD369KS/A` (A1430 Wi-Fi + Cellular)
- **OS:** Apple iOS 9.3.6 (WebKit 601.1.46)
- **Constraint:** Modern frameworks (React, Vue, Tailwind, ES6 arrow functions, async/await, CSS Grid) crash or render blank pages on iOS 9 Mobile Safari.

### Solution:
`static/ipad.html` is constructed with **pure ECMAScript 5 (ES5)** and hardware-accelerated CSS3:
1. Standard `var` declarations, `XMLHttpRequest`, and anonymous function callbacks.
2. WebKit vendor prefixes (`-webkit-overflow-scrolling: touch;`, `-webkit-backdrop-filter: blur(8px);`).
3. Web Audio API with fallback and persistent touch-unlock for chime notifications.
4. Auto-reconnection engine (`visibilitychange` and `focus` event listeners).

---

## 🚀 5. Automated Versioning & Deployment Pipeline

Version bumping and live server deployment are fully automated through `scripts/bump_version.py`:

```powershell
# Bump to a new release and deploy to H96 Max
python scripts/bump_version.py 3.11.0 "Cyberpunk Boot Splash Screen & UI Experience"

# Synchronize all dashboards from changelog.json
python scripts/bump_version.py --sync
```

### What the pipeline executes automatically:
1. **Reads Single Source of Truth:** `changelog.json`.
2. **Generates HTML Cards:** Formats and injects release cards into `static/index.html` (`#tab-changelog`) and `static/ipad.html` (`#ipad-tab-ver`).
3. **Updates Metadata & Badges:** `version.py`, `README.md`, `README.ru.md`, `index.html`, and `index.en.html`.
4. **Bumps Cache Busters:** Updates `dashboard.css?v=...` and `dashboard.js?v=...`.
5. **Probes H96 Max:** Tests SSH reachability on `192.168.0.103:8022`.
6. **Deploys via SCP:** Securely copies all modified files to `~/Samsung_RT34MB_Monitor/`.
7. **Triggers Supervisor Restart:** Signals `pkill -f 'auto_monitor.py'`, allowing the supervisor to reboot the service within 10 seconds.
