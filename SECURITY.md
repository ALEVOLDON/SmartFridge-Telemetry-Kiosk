# Security Policy & Operational Guidelines

## Threat Model & Scope

**SmartFridge-Telemetry-Kiosk** is intended strictly for **Private Local Area Network (LAN)** use within trusted home environments.

### ⚠️ Critical Network Safety Rules:
1. **DO NOT EXPOSE PORT 8088 TO THE PUBLIC INTERNET (WAN):**
   - Do not create port forwarding rules on your router for port `8088`.
   - Never place this server in a DMZ zone.
   - The Flask app rejects requests whose client IP is not loopback, RFC1918, link-local, or CGNAT (Tailscale `100.64.0.0/10`).
2. **Remote Access:**
   - If you need to monitor your refrigerator outside your home Wi-Fi network, use a secure private VPN such as **Tailscale**, **WireGuard**, or a reverse proxy (e.g., **Nginx / Caddy / Cloudflare Tunnel**) protected with TLS and HTTP Basic Authentication.
3. **Tuya API Credentials:**
   - Your `config.json` file contains your private Tuya IoT OpenAPI keys.
   - The `/api/config` REST endpoint automatically masks secrets over the network and disallows configuration changes from non-localhost IPs.
   - `/api/tariff` POST is allowed from the LAN (phone dashboard) but not from public IPs.
   - Never commit `config.json`, `lan.env`, or database files to public version control.
4. **LAN subnet:**
   - Set `lan_subnet` in `config.json` (and optionally `lan.env` for Termux scripts) if the home network is not `192.168.0.0/24`.

## Reporting a Security Vulnerability

If you discover any security vulnerabilities or credential leakage risks in this open-source project, please open a private GitHub Security Advisory or submit an issue.
