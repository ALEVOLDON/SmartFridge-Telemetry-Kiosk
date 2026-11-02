---
description: Critical hardware topology, power chain, stabilizer setup, and telemetry signatures for Samsung RT34MB refrigerator
always_on: true
---

# Samsung RT34MB Telemetry & Hardware Rules

When assisting with the `Samsung_RT34MB_Monitor` project, strictly follow these hardware facts:

1. **Power Chain Topology (MANDATORY & LIVE STATUS):**
   * **АКТУАЛЬНАЯ ЦЕПЬ (с 02.11.2026):**
     `Mains 220V (просадки до 170-175V)` ➔ `Defender 3m cable` ➔ `RUCELF SRW-16A Relay` ➔ `Voltage Stabilizer` ➔ `Samsung RT34MB Fridge` (напрямую).
   * **Статус Tuya Smart Plug (192.168.0.104):**
     **ВРЕМЕННО СНЯТА ИЗ ЦЕПИ** из-за аномально низкой сети (170 В) и помех от ступеней стабилизатора, вызывавших аппаратный клин измерительного чипа (BL0942 UART latch-up). Новую розетку НЕ покупать — старая исправна и вернется в строй после стабилизации сети.
   * **Базовая полная цепь:**
     `Mains` ➔ `Defender 3m` ➔ `RUCELF Relay` ➔ `Stabilizer` ➔ `Tuya Plug (192.168.0.104)` ➔ `Fridge`.

2. **RUCELF SRW-16A Relay Settings:**
   - `H = 255 V` (Overvoltage surge cutoff).
   - `L = 150 V` (**CORRECT SETTING**: Because the stabilizer follows the relay! The stabilizer handles 150–250V input and steps it up to 220–230V. Never advise raising L to 180V+ unless the stabilizer is removed).
   - `S = 300 s` (5-minute startup delay for refrigerant equalization after blackouts).

3. **Fridge Electromechanics (Samsung RT34MB):**
   - **No digital boards or microcontrollers exist in this fridge.** It is 100% electromechanical.
   - Motor: Samsung SK170K-T1U TS1 (200-220V, 0.70A, LRA 5.8A, R-134a 160g).
   - Thermostat: PFN-C174S-03EB-1.
   - Defrost Timer: TMDEX09UC1 (triggers every 8-10h of cooling for 15-25 min).

4. **Telemetry Signatures:**
   - **Cooling (Compressor):** 130–145 W, 0.85–1.05 A, $\cos\varphi \approx 0.60\dots0.75$ (inductive motor).
   - **Defrost (No Frost Heater):** 155–170 W (flat ~169 W), ~0.73 A, $\cos\varphi \approx 1.00$ (pure resistive heating element). **COMPRESSOR IS OFF AND SILENT DURING DEFROST BY DESIGN.**
   - **Idle:** 0 W, 0 A (thermostat satisfied).

5. **Server Infrastructure:**
   - 24/7 Server: Android TV Box H96 Max (`192.168.0.103:8088`), Termux + Termux:Boot (`scripts/h96_start_fridge.sh`).
   - If `config.json` is corrupted after hard blackout, restore via ADB: `adb -s 192.168.0.103:5555 push config.json /sdcard/` and copy with `su 0` to Termux dir.
