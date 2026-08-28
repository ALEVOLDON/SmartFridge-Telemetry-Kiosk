@echo off
:: Open Samsung Smart Fridge Monitor pointing to 24/7 H96 Max Server
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --app="http://192.168.0.104:8088" --window-size=1250,880
) else (
    start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --app="http://192.168.0.104:8088" --window-size=1250,880
)

