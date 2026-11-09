# راهنمای عملیاتی اجرای خودکار در پس‌زمینه (Windows 11 و Linux)

این مستند نحوه تنظیم اجرای خودکار **Samad Food Monitor** پس از روشن شدن یا ری‌استارت سیستم را به‌صورت گام‌به‌گام شرح می‌دهد.

---

## بخش اول: راه‌اندازی در Windows 11 با Windows Task Scheduler (گزینه پیشنهادی اصلی)

### چرا این روش بهترین گزینه در ویندوز ۱۱ است؟
1. استفاده از `pythonw.exe` باعث می‌شود برنامه **بدون هیچ پنجره سیاه CMD/PowerShell** کاملاً در پس‌زمینه اجرا شود.
2. ثبت Task در حالت `Interactive` (نشست کاربر فعلی) تضمین می‌کند که **اعلان‌های دسکتاپ ویندوز ۱۱ (Toast Notifications)** به‌درستی در گوشه تصویر و Action Center نمایش داده شوند (اگر Task در حالت Session 0 ثبت شود، ویندوز نمایش اعلان گرافیکی را مسدود می‌کند).
3. محدودیت پیش‌فرض ۷۲ ساعته Task Scheduler حذف شده و روی لپ‌تاپ در حالت باتری نیز متوقف نمی‌شود.

### پیش‌نیاز پیش از ثبت سرویس
مطمئن شوید که یک بار ورود دستی به سماد را انجام داده‌اید:
```powershell
.\.venv\Scripts\python.exe -m samad_monitor login
.\.venv\Scripts\python.exe -m samad_monitor check-now
```

### روش ۱: نصب خودکار با اسکریپت PowerShell (پیشنهادی — یک دستور)
در پوشه اصلی پروژه، PowerShell را باز کرده و اجرا کنید:
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_task.ps1
```

#### بررسی وضعیت اجرای سرویس در ویندوز ۱۱:
```powershell
Get-ScheduledTask -TaskName "SamadFoodMonitor" | Select-Object TaskName, State
.\.venv\Scripts\python.exe -m samad_monitor status
```

#### توقف یا حذف سرویس خودکار در ویندوز ۱۱:
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_windows_task.ps1
```

---

### روش ۲: تنظیم دستی از طریق رابط گرافیکی Task Scheduler در ویندوز ۱۱
اگر مایل به ساخت دستی Task هستید:
1. کلید `Win + R` را بزنید، عبارت `taskschd.msc` را وارد و `Enter` کنید.
2. از پنل سمت راست گزینه **Create Task...** (نه Create Basic Task) را بزنید:
   - **تب General**:
     - `Name`: `SamadFoodMonitor`
     - گزینه **Run only when user is logged on** را انتخاب کنید (الزامی برای نمایش نوتیفیکیشن دسکتاپ ویندوز ۱۱).
   - **تب Triggers**:
     - روی **New...** کلیک کنید و `Begin the task` را روی **At log on** و `Specific user` (نام کاربری خودتان) بگذارید.
   - **تب Actions**:
     - روی **New...** کلیک کنید:
       - `Program/script`: مسیر کامل `C:\path\to\samad-food-monitor\.venv\Scripts\pythonw.exe`
       - `Add arguments`: `-m samad_monitor run`
       - `Start in`: مسیر پوشه پروژه `C:\path\to\samad-food-monitor`
   - **تب Conditions**:
     - تیک **Start the task only if the computer is on AC power** و **Stop if the computer switches to battery power** را **بردارید**.
   - **تب Settings**:
     - تیک **Run task as soon as possible after a scheduled start is missed** را بزنید.
     - تیک **Stop the task if it runs longer than: 3 days** را **بردارید**.
     - تیک **If the task fails, restart every** را روی `2 minutes` بگذارید.
3. روی **OK** کلیک کنید، سپس روی نام Task راست‌کلیک کرده و **Run** را بزنید.

---

## بخش دوم: راه‌اندازی در لینوکس با `systemd --user`

در توزیع‌های مدرن لینوکس (Ubuntu / Debian / Fedora / Arch):

1. مطمئن شوید روی دسکتاپ محلی خود با `login` وارد شده‌اید و کانال `telegram` در فایل `.env` فعال است.
2. اسکریپت نصب سرویس کاربری را اجرا کنید:
```bash
chmod +x scripts/install_linux_systemd.sh
./scripts/install_linux_systemd.sh
```
3. اگر می‌خواهید سرویس حتی پیش از لاگین گرافیکی بعد از ریبوت نیز فعال بماند:
```bash
sudo loginctl enable-linger "$USER"
```
4. دستورات مدیریت سرویس در لینوکس:
```bash
# بررسی وضعیت سرویس
systemctl --user status samad-monitor.service

# مشاهده زنده لاگ‌ها
journalctl --user -u samad-monitor.service -f

# راه‌اندازی مجدد سرویس پس از تغییر .env
systemctl --user restart samad-monitor.service

# توقف و غیرفعال‌سازی
systemctl --user disable --now samad-monitor.service
```
