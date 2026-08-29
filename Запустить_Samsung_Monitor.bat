@echo off
setlocal enabledelayedexpansion

:: Smart Auto-Discovery Launcher for Samsung Smart Fridge Monitor
set TARGET_URL=

:: 1. Check if H96 Max is at 192.168.0.103
powershell -Command "$s = New-Object Net.Sockets.TcpClient; try { $s.Connect(\"192.168.0.103\", 8088); exit 0 } catch { exit 1 }"
if %errorlevel% equ 0 (
    set TARGET_URL=http://192.168.0.103:8088
    goto OPEN_URL
)

:: 2. Check if H96 Max is at 192.168.0.104
powershell -Command "$s = New-Object Net.Sockets.TcpClient; try { $s.Connect(\"192.168.0.104\", 8088); exit 0 } catch { exit 1 }"
if %errorlevel% equ 0 (
    set TARGET_URL=http://192.168.0.104:8088
    goto OPEN_URL
)

:: 3. Fallback to Localhost on PC
netstat -ano | findstr :8088 >nul
if %errorlevel% neq 0 (
    start "" /b python "C:\Users\alevo\Desktop\Samsung_RT34MB_Monitor\auto_monitor.py" --host 0.0.0.0 --port 8088
    timeout /t 2 /nobreak >nul
)
set TARGET_URL=http://localhost:8088

:OPEN_URL
echo Opening Samsung Smart Fridge Monitor at %TARGET_URL%...
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --app="%TARGET_URL%" --window-size=1250,880
) else (
    start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --app="%TARGET_URL%" --window-size=1250,880
)

