#!/usr/bin/env python3
"""Automated Version Bumper & Changelog Synchronizer for SmartFridge-Telemetry-Kiosk.

Single Source of Truth: changelog.json

Usage:
    python scripts/bump_version.py <version> [title] [date] [--deploy]
    python scripts/bump_version.py --sync [--deploy]

Examples:
    python scripts/bump_version.py 3.11.0 "Cyberpunk Boot Splash Screen & UI Experience" "26.09.2026"
    python scripts/bump_version.py --sync
    python scripts/bump_version.py 3.11.0 --deploy
"""

from datetime import datetime
import json
import os
import re
import subprocess
import sys
import io

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG_PATH = os.path.join(BASE_DIR, "changelog.json")
H96_HOST = "u0_a140@192.168.0.103"
H96_PORT = "8022"
H96_DIR = "~/Samsung_RT34MB_Monitor"


def load_changelog():
    if not os.path.exists(CHANGELOG_PATH):
        raise FileNotFoundError(f"Changelog not found at {CHANGELOG_PATH}")
    with open(CHANGELOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_changelog(data):
    with open(CHANGELOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def escape_html(text: str) -> str:
    """Escapes HTML entities safely while preserving code tags."""
    if not text:
        return ""
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Restore allowed inline formatting if present
    safe = safe.replace("&lt;code&gt;", "<code>").replace("&lt;/code&gt;", "</code>")
    return safe


def is_pro_version(ver: str) -> bool:
    clean = ver.lstrip("v").strip()
    parts = clean.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        return major > 3 or (major == 3 and minor >= 5)
    except Exception:
        return True


def generate_index_changelog_html(changelog_data) -> str:
    """Builds HTML cards for static/index.html (#tab-changelog)."""
    cards = []
    for item in changelog_data:
        ver = item.get("version", "").lstrip("v").strip()
        is_pro = is_pro_version(ver)
        pro_label = " Pro" if is_pro else ""
        title = item.get("title", "").strip()
        ver_header = f"v{ver}{pro_label} — {title}" if title else f"v{ver}{pro_label}"
        date = item.get("date", "").strip()

        lines = [
            '                <div class="changelog-card">',
            '                    <div class="changelog-ver">',
            f'                        <span>{escape_html(ver_header)}</span>',
            f'                        <span class="changelog-date">{escape_html(date)}</span>',
            '                    </div>'
        ]
        for pt in item.get("points", []):
            lead = escape_html(pt.get("lead", "").strip())
            desc = escape_html(pt.get("desc", "").strip())
            lines.append(f'                    <div class="changelog-point"><strong>{lead}:</strong> {desc}</div>')
        lines.append('                </div>')
        cards.append("\n".join(lines))
    return "\n\n".join(cards)


def generate_ipad_changelog_html(changelog_data) -> str:
    """Builds HTML cards for static/ipad.html (#ipad-tab-ver) in pure ES5 / WebKit format."""
    cards = []
    for i, item in enumerate(changelog_data):
        ver = item.get("version", "").lstrip("v").strip()
        is_pro = is_pro_version(ver)
        pro_label = " Pro" if is_pro else ""
        date = item.get("date", "").strip()

        is_latest = (i == 0)
        border_color = "#10b981" if is_latest else "#38bdf8"
        status_suffix = " — Актуальная версия" if is_latest else ""
        margin = " margin-bottom:10px;" if i < len(changelog_data) - 1 else ""

        lines = [
            f'                    <div style="background:#0f172a; border-radius:10px; padding:12px; border:1px solid #334155; border-left:4px solid {border_color};{margin}">',
            f'                        <div style="font-weight:bold; color:{border_color};">v{ver}{pro_label} ({date}){status_suffix}</div>'
        ]
        for pt in item.get("points", []):
            lead = escape_html(pt.get("lead", "").strip())
            desc = escape_html(pt.get("desc", "").strip())
            lines.append(f'                        <div>• {lead}: {desc}</div>')
        lines.append('                    </div>')
        cards.append("\n".join(lines))
    return "\n\n".join(cards)


def is_h96_reachable() -> bool:
    """Probes H96 Max via SSH connection with short timeout."""
    try:
        cmd = ["ssh", "-p", H96_PORT, "-o", "ConnectTimeout=2", "-o", "BatchMode=yes", f"{H96_HOST}", "echo 1"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        return res.returncode == 0
    except Exception:
        return False


def deploy_to_h96():
    """Deploys updated core files and static assets to H96 Max via SCP and triggers restart."""
    print(f"\n[DEPLOY] Deploying to H96 Max ({H96_HOST}:{H96_PORT})...")

    # Core server files
    core_files = ["changelog.json", "version.py", "auto_monitor.py", "fridge_logic.py", "index.html", "index.en.html"]
    for cf in core_files:
        local_p = os.path.join(BASE_DIR, cf)
        if os.path.exists(local_p):
            cmd = ["scp", "-P", H96_PORT, local_p, f"{H96_HOST}:{H96_DIR}/{cf}"]
            subprocess.run(cmd, check=True)
            print(f"  [SCP] Copied {cf}")

    # Static assets
    static_files = [
        os.path.join("static", "index.html"),
        os.path.join("static", "ipad.html"),
        os.path.join("static", "css", "dashboard.css"),
        os.path.join("static", "js", "dashboard.js"),
        os.path.join("static", "js", "analytics.js"),
    ]
    for sf in static_files:
        local_p = os.path.join(BASE_DIR, sf)
        remote_rel = sf.replace("\\", "/")
        if os.path.exists(local_p):
            cmd = ["scp", "-P", H96_PORT, local_p, f"{H96_HOST}:{H96_DIR}/{remote_rel}"]
            subprocess.run(cmd, check=True)
            print(f"  [SCP] Copied {sf}")

    # Trigger restart so auto_monitor reloads version.py & changelog.json
    try:
        restart_cmd = ["ssh", "-p", H96_PORT, f"{H96_HOST}", "pkill -f 'auto_monitor.py'"]
        subprocess.run(restart_cmd, timeout=4)
        print("  [RESTART] Signaled auto_monitor restart on H96 Max (supervisor will boot it within seconds)")
    except Exception as e:
        print("  [WARN] Restart signal note:", e)

    print("  [SUCCESS] Deployment to H96 Max complete!")


def sync_all(clean_ver: str, release_title: str, today_str: str, changelog_data: list, auto_deploy: bool = False):
    ver_str = f"PRO v{clean_ver}"
    print(f"\n[SYNC] Synchronizing all files to {clean_ver} ({ver_str}) - {today_str}...")

    # 1. Update version.py
    version_py_path = os.path.join(BASE_DIR, "version.py")
    version_py_content = f'''"""Single Source of Truth for SmartFridge-Telemetry-Kiosk version metadata."""

VERSION = "{clean_ver}"
VERSION_STRING = "{ver_str}"
RELEASE_TITLE = "{release_title}"
RELEASE_DATE = "{today_str}"
'''
    with open(version_py_path, "w", encoding="utf-8") as f:
        f.write(version_py_content)
    print("  [OK] Updated version.py")

    # 2. Update landing pages: index.html & index.en.html
    for landing_page in ["index.html", "index.en.html"]:
        landing_path = os.path.join(BASE_DIR, landing_page)
        if os.path.exists(landing_path):
            with open(landing_path, "r", encoding="utf-8") as f:
                content = f.read()

            content = re.sub(
                r'<span class="badge-pro">PRO v[\d\.]+</span>',
                f'<span class="badge-pro">{ver_str}</span>',
                content,
            )
            content = re.sub(
                r'<span>(Релиз|Release) v[\d\.]+ Pro • (.*?)</span>',
                f'<span>\\1 v{clean_ver} Pro • {today_str}</span>',
                content,
            )
            content = re.sub(
                r'releases/tag/v[\d\.]+',
                f'releases/tag/v{clean_ver}',
                content,
            )
            content = re.sub(
                r'(Скачать релиз|Скачать|Download) v[\d\.]+',
                f'\\1 v{clean_ver}',
                content,
            )
            content = re.sub(
                r'Samsung RT34MB Intelligent Energy Monitor • Pro Edition v[\d\.]+',
                f'Samsung RT34MB Intelligent Energy Monitor • Pro Edition v{clean_ver}',
                content,
            )
            with open(landing_path, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"  [OK] Updated {landing_page}")

    # 3. Update static/index.html (changelog cards, modal badge, splash badge, cache-busters)
    static_index_path = os.path.join(BASE_DIR, "static", "index.html")
    if os.path.exists(static_index_path):
        with open(static_index_path, "r", encoding="utf-8") as f:
            c = f.read()

        c = re.sub(r'dashboard\.css\?v=[\d\.]+', f'dashboard.css?v={clean_ver}', c)
        c = re.sub(r'dashboard\.js\?v=[\d\.]+', f'dashboard.js?v={clean_ver}', c)
        c = re.sub(r'analytics\.js\?v=[\d\.]+', f'analytics.js?v={clean_ver}', c)
        c = re.sub(
            r'<span class="about-pro-badge">PRO v[\d\.]+</span>',
            f'<span class="about-pro-badge">{ver_str}</span>',
            c,
        )
        c = re.sub(
            r'TELEMETRY KIOSK • PRO v[\d\.]+',
            f'TELEMETRY KIOSK • {ver_str}',
            c,
        )

        # Inject rendered changelog cards into #tab-changelog
        index_cards_html = generate_index_changelog_html(changelog_data)
        c = re.sub(
            r'(<div id="tab-changelog"[^>]*>)([\s\S]*?)(</div>\s*<!-- Tab 4: Specs -->)',
            f'\\1\n{index_cards_html}\n            \\3',
            c,
        )

        with open(static_index_path, "w", encoding="utf-8") as f:
            f.write(c)
        print("  [OK] Updated static/index.html (cards & cache-busters)")

    # 4. Update static/ipad.html (changelog cards & modal badge)
    ipad_path = os.path.join(BASE_DIR, "static", "ipad.html")
    if os.path.exists(ipad_path):
        with open(ipad_path, "r", encoding="utf-8") as f:
            c_ipad = f.read()

        c_ipad = re.sub(
            r'(<span id="ipad-version-badge"[^>]*>)PRO v[\d\.]+</span>',
            f'\\1{ver_str}</span>',
            c_ipad,
        )
        c_ipad = re.sub(
            r'Samsung RT34MB Monitor <span id="ipad-version-badge"[^>]*>PRO v[\d\.]+</span>',
            f'Samsung RT34MB Monitor <span id="ipad-version-badge" style="font-size:10px; background:#0284c7; color:#fff; padding:1px 6px; border-radius:4px;">{ver_str}</span>',
            c_ipad,
        )

        # Inject rendered changelog cards into #ipad-tab-ver
        ipad_cards_html = generate_ipad_changelog_html(changelog_data)
        c_ipad = re.sub(
            r'(<div id="ipad-tab-ver"[^>]*>)([\s\S]*?)(</div>\s*<!-- Tab: Specs -->)',
            f'\\1\n{ipad_cards_html}\n                \\3',
            c_ipad,
        )

        with open(ipad_path, "w", encoding="utf-8") as f:
            f.write(c_ipad)
        print("  [OK] Updated static/ipad.html (cards & version badge)")

    # 5. Update README badges & changelog range
    for readme_fname, pattern, repl in [
        ("README.md", r'Release-PRO%20v[\d\.]+', f'Release-PRO%20v{clean_ver}'),
        ("README.ru.md", r'Релиз-PRO%20v[\d\.]+', f'Релиз-PRO%20v{clean_ver}'),
    ]:
        readme_path = os.path.join(BASE_DIR, readme_fname)
        if os.path.exists(readme_path):
            with open(readme_path, "r", encoding="utf-8") as f:
                r_content = f.read()
            r_content = re.sub(pattern, repl, r_content)
            r_content = re.sub(r'(to|до) `PRO v[\d\.]+`', f'\\1 `{ver_str}`', r_content)
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(r_content)
            print(f"  [OK] Updated {readme_fname}")

    print(f"\n[SUCCESS] All local files successfully synchronized to v{clean_ver} ({release_title})!")

    # 6. Deployment check
    if auto_deploy or is_h96_reachable():
        deploy_to_h96()
    else:
        print(f"  [INFO] H96 Max not reachable at {H96_HOST}:{H96_PORT}. Skipping remote deployment.")


def main():
    args = sys.argv[1:]
    deploy_requested = "--deploy" in args
    args = [a for a in args if a != "--deploy"]

    changelog_data = load_changelog()
    if not changelog_data:
        print("Error: changelog.json is empty!")
        sys.exit(1)

    if not args or args[0] == "--sync":
        # Synchronize from latest entry in changelog.json
        latest = changelog_data[0]
        clean_ver = latest["version"].lstrip("v").strip()
        release_title = latest.get("title", f"v{clean_ver} Release")
        today_str = latest.get("date", datetime.now().strftime("%d.%m.%Y"))
        sync_all(clean_ver, release_title, today_str, changelog_data, auto_deploy=deploy_requested)
        return

    ver_arg = args[0].lstrip("v").strip()
    title_arg = args[1] if len(args) > 1 else ""
    date_arg = args[2] if len(args) > 2 else ""

    # Check if entry already exists in changelog
    found = None
    for entry in changelog_data:
        if entry.get("version", "").lstrip("v").strip() == ver_arg:
            found = entry
            break

    today_str = date_arg or datetime.now().strftime("%d.%m.%Y")
    if found:
        if title_arg:
            found["title"] = title_arg
        if date_arg:
            found["date"] = date_arg
        release_title = found.get("title", f"v{ver_arg} Pro Release")
        today_str = found.get("date", today_str)
        print(f"Updating existing changelog entry for v{ver_arg}: {release_title}")
    else:
        release_title = title_arg or f"v{ver_arg} Pro Release"
        new_entry = {
            "version": ver_arg,
            "title": release_title,
            "date": today_str,
            "points": [
                {
                    "lead": "Стабильный релиз",
                    "desc": "комплексное обновление подсистем телеметрии и интерфейса."
                }
            ]
        }
        changelog_data.insert(0, new_entry)
        print(f"Created new changelog entry for v{ver_arg}: {release_title}")

    save_changelog(changelog_data)
    sync_all(ver_arg, release_title, today_str, changelog_data, auto_deploy=deploy_requested)


if __name__ == "__main__":
    main()
