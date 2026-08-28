@echo off
cd /d "%~dp0"

:: Check if server is running on port 8088, start if needed
netstat -ano | findstr :8088 >nul
if %errorlevel% neq 0 (
    start "" /b python auto_monitor.py
    timeout /t 1 /nobreak >nul
)

:: Open in Standalone Native App Window
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --app="http://localhost:8088" --window-size=1250,880
) else (
    start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --app="http://localhost:8088" --window-size=1250,880
)
