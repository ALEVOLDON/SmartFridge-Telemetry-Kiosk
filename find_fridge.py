import sys
import socket
import urllib.request
import json
import concurrent.futures
import subprocess
import os

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

def check_host(ip):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.35)
        res = s.connect_ex((ip, 8088))
        s.close()
        if res == 0:
            url = f"http://{ip}:8088/api/status"
            req = urllib.request.urlopen(url, timeout=1.0)
            data = json.loads(req.read().decode('utf-8'))
            if "power" in data or "is_running" in data or "temp_freezer" in data:
                return (ip, data)
    except Exception:
        pass
    return None

def find_fridge_server():
    print("=" * 60)
    print(" [~] СКАНИРОВАНИЕ СЕТИ НА ПОИСК SAMSUNG RT34MB...")
    print("=" * 60)
    
    # 1. Parallel scan subnet
    ips = [f"192.168.0.{i}" for i in range(100, 130)]
    found = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        results = executor.map(check_host, ips)
        for r in results:
            if r:
                found.append(r)
                
    local_res = check_host("127.0.0.1")
    return found, local_res

if __name__ == "__main__":
    found, local_res = find_fridge_server()
    
    target_ip = None
    if found:
        target_ip, data = found[0]
        print(f"\n[+] ПРИСТАВКА H96 MAX НАЙДЕНА В СЕТИ!")
        print(f"    IP-адрес:   {target_ip}")
        print(f"    Мощность:   {data.get('power', 0.0)} Вт | Напряжение: {data.get('voltage', 0.0)} В")
        print(f"    Статус:     {data.get('mode_title', 'Норма')}")
        print("-" * 60)
        print(f" [!] ССЫЛКА ДЛЯ IPAD НА ХОЛОДИЛЬНИКЕ:")
        print(f"     👉 http://{target_ip}:8088/ipad")
        print("-" * 60)
        print(f" [!] ССЫЛКА ДЛЯ КОМПЬЮТЕРА:")
        print(f"     👉 http://{target_ip}:8088")
        print("=" * 60)
    elif local_res:
        target_ip = "127.0.0.1"
        print(f"\n[+] НАЙДЕН ЛОКАЛЬНЫЙ СЕРВЕР НА ПК:")
        print(f"    👉 http://localhost:8088")
    else:
        print("\n[-] Сервер не найден в диапазоне 192.168.0.100-130.")
        print("    Проверьте, включена ли приставка в розетку и подключена ли к Wi-Fi.")
        target_ip = "localhost"

    if len(sys.argv) > 1 and sys.argv[1] == "--open" and target_ip:
        target_url = f"http://{target_ip}:8088"
        chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        if os.path.exists(chrome):
            subprocess.Popen([chrome, f"--app={target_url}", "--window-size=1250,880"])
        elif os.path.exists(edge):
            subprocess.Popen([edge, f"--app={target_url}", "--window-size=1250,880"])
