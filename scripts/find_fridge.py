import sys
import socket
import urllib.request
import json
import concurrent.futures
import subprocess
import os

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_IP_FILE = os.path.join(APP_DIR, "last_server_ip.txt")
PORT = 8088
HOME_PREFIX = "192.168.0."


def load_last_ip():
    try:
        with open(LAST_IP_FILE, "r", encoding="utf-8") as f:
            ip = f.read().strip()
        if ip and ip.count(".") == 3:
            return ip
    except Exception:
        pass
    return None


def save_last_ip(ip):
    try:
        with open(LAST_IP_FILE, "w", encoding="utf-8") as f:
            f.write(ip)
    except Exception:
        pass


def check_host(ip):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.45)
        res = s.connect_ex((ip, PORT))
        s.close()
        if res != 0:
            return None
        req = urllib.request.urlopen(f"http://{ip}:{PORT}/api/status", timeout=1.2)
        data = json.loads(req.read().decode("utf-8"))
        if "power" in data or "is_running" in data or "temp_freezer" in data:
            return (ip, data)
    except Exception:
        pass
    return None


def scan_home_lan(skip_ips):
    ips = [f"{HOME_PREFIX}{i}" for i in range(2, 255) if f"{HOME_PREFIX}{i}" not in skip_ips]
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
        for r in executor.map(check_host, ips):
            if r:
                found.append(r)
    return found


def find_fridge_server():
    print("=" * 60)
    print(" [~] ПОИСК SAMSUNG RT34MB В ДОМАШНЕЙ СЕТИ 192.168.0.x")
    print("=" * 60)

    tried = set()
    quick = []
    last_ip = load_last_ip()
    if last_ip:
        quick.append(last_ip)
    for hint in ("192.168.0.103", "192.168.0.104", "192.168.0.102", "127.0.0.1"):
        if hint not in quick:
            quick.append(hint)

    for ip in quick:
        print(f" [~] Проверка {ip}...")
        hit = check_host(ip)
        tried.add(ip)
        if hit:
            return [hit], None

    print(f" [~] Сканирование {HOME_PREFIX}2–254...")
    found = scan_home_lan(tried)
    local_res = check_host("127.0.0.1")
    return found, local_res


def open_kiosk(url):
    browsers = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in browsers:
        if os.path.exists(path):
            subprocess.Popen([path, f"--app={url}", "--window-size=1250,880"])
            return True
    try:
        os.startfile(url)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    found, local_res = find_fridge_server()
    want_open = len(sys.argv) > 1 and sys.argv[1] == "--open"

    target_ip = None
    data = None
    if found:
        target_ip, data = found[0]
    elif local_res:
        target_ip, data = local_res

    if not target_ip:
        print("\n[-] Сервер не найден в 192.168.0.2–254.")
        print("    Приставка H96 должна быть в домашнем Wi-Fi.")
        sys.exit(1)

    save_last_ip(target_ip)
    print(f"\n[+] СЕРВЕР: http://{target_ip}:{PORT}")
    print(f"    Мощность: {data.get('power', 0.0)} Вт | {data.get('mode_title', '')}")
    print(f"    iPad:     http://{target_ip}:{PORT}/ipad")

    if want_open:
        url = f"http://{target_ip}:{PORT}"
        if not open_kiosk(url):
            print("[-] Chrome/Edge не найден.")
            sys.exit(1)
        print(f"[+] Окно монитора: {url}")
