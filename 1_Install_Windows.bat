@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ======================================================================
echo   Samad Food Monitor - نصب و آماده سازی خودکار در ویندوز 11
echo ======================================================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [خطا] پایتون روی سیستم شما نصب نیست یا به PATH اضافه نشده است!
    echo لطفا Python 3.11+ را از سایت python.org نصب کرده و تیک Add Python to PATH را بزنید.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] در حال ساخت محیط مجازی پایتون (.venv)...
    python -m venv .venv
)

echo [2/4] در حال نصب کتابخانه های مورد نیاز...
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -r requirements.txt
.\.venv\Scripts\pip.exe install -e .

echo [3/4] بررسی مرورگر سیستم...
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
    echo [تایید] مرورگر Google Chrome شناسایی شد؛ نیازی به دانلود کرومیوم نیست!
) else if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" (
    echo [تایید] مرورگر Microsoft Edge ویندوز 11 شناسایی شد؛ نیازی به دانلود کرومیوم نیست!
) else if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" (
    echo [تایید] مرورگر Microsoft Edge ویندوز 11 شناسایی شد؛ نیازی به دانلود کرومیوم نیست!
) else (
    echo [3/4] دانلود کرومیوم از سرور آینه بدون تحریم...
    set PLAYWRIGHT_DOWNLOAD_HOST=https://registry.npmmirror.com/-/binary/playwright
    .\.venv\Scripts\playwright.exe install chromium
)

if not exist ".env" (
    echo [4/4] ساخت فایل تنظیمات اولیه (.env)...
    copy /Y ".env.example" ".env" >nul
)

echo.
echo ======================================================================
echo   نصب با موفقیت کامل شد!
echo   اکنون روی فایل 2_Start_Dashboard_Windows.bat دوبار کلیک کنید.
echo ======================================================================
pause
