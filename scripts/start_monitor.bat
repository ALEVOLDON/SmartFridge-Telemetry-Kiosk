@echo off
cd /d "%~dp0"
set "PY="
if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PY=%LocalAppData%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PY=%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PY set "PY=py"

"%PY%" "%~dp0find_fridge.py" --open
if errorlevel 1 (
    start "Samsung RT34MB" cmd /k "echo Сервер холодильника не найден в 192.168.0.x & echo Включите приставку H96 Max в домашнем Wi-Fi. & pause"
    exit /b 1
)
exit /b 0
