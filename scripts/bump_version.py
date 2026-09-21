#!/usr/bin/env python3
"""Automated Version Bumper for SmartFridge-Telemetry-Kiosk.

Usage:
    python scripts/bump_version.py <version> [title] [date]

Example:
    python scripts/bump_version.py 3.9.0 "Dual-Tier AI (Gemini Flash & TypeSafe) & Cycle Resilience"
"""

from datetime import datetime
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bump_version(ver_num: str, title: str = "", date_str: str = ""):
    clean_ver = ver_num.lstrip("v").strip()
    ver_str = f"PRO v{clean_ver}"
    today_str = date_str or datetime.now().strftime("%d.%m.%Y")
    release_title = title or f"v{clean_ver} Pro Release"

    print(f"Bumping version to {clean_ver} ({ver_str}) - {today_str}...")

    # 1. Update version.py (Single Source of Truth)
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

    # 2. Update index.html
    index_html_path = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(index_html_path):
        with open(index_html_path, "r", encoding="utf-8") as f:
            content = f.read()

        content = re.sub(
            r'<span class="badge-pro">PRO v[\d\.]+</span>',
            f'<span class="badge-pro">{ver_str}</span>',
            content,
        )
        content = re.sub(
            r'<span>Релиз v[\d\.]+ Pro • (.*?)</span>',
            f'<span>Релиз v{clean_ver} Pro • \\1</span>',
            content,
        )
        content = re.sub(
            r'releases/tag/v[\d\.]+',
            f'releases/tag/v{clean_ver}',
            content,
        )
        content = re.sub(
            r'📦 Скачать релиз v[\d\.]+',
            f'📦 Скачать релиз v{clean_ver}',
            content,
        )
        content = re.sub(
            r'Samsung RT34MB Intelligent Energy Monitor • Pro Edition v[\d\.]+',
            f'Samsung RT34MB Intelligent Energy Monitor • Pro Edition v{clean_ver}',
            content,
        )
        with open(index_html_path, "w", encoding="utf-8") as f:
            f.write(content)
        print("  [OK] Updated index.html")

    # 3. Update static/index.html (cache busters and modal badge)
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
        with open(static_index_path, "w", encoding="utf-8") as f:
            f.write(c)
        print("  [OK] Updated static/index.html")

    # 4. Update static/ipad.html
    ipad_path = os.path.join(BASE_DIR, "static", "ipad.html")
    if os.path.exists(ipad_path):
        with open(ipad_path, "r", encoding="utf-8") as f:
            c_ipad = f.read()

        c_ipad = re.sub(
            r'PRO v[\d\.]+</span>',
            f'{ver_str}</span>',
            c_ipad,
            count=1,
        )
        with open(ipad_path, "w", encoding="utf-8") as f:
            f.write(c_ipad)
        print("  [OK] Updated static/ipad.html")

    print(f"\nAll files successfully bumped to {clean_ver}!")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/bump_version.py <version> [title] [date]")
        sys.exit(1)
    v = sys.argv[1]
    t = sys.argv[2] if len(sys.argv) > 2 else ""
    d = sys.argv[3] if len(sys.argv) > 3 else ""
    bump_version(v, t, d)
