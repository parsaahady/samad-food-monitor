@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ابتدا فایل 1_Install_Windows.bat را یک بار اجرا کنید!
    pause
    exit /b 1
)

echo در حال باز کردن پنل گرافیکی و فعال سازی پایش خودکار سماد (دانشگاه شهید بهشتی)...
.\.venv\Scripts\python.exe -m samad_monitor ui --port 8765 --auto-start
