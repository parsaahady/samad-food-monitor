<#
.SYNOPSIS
    نصب خودکار سرویس پس‌زمینه Samad Food Monitor در Windows 11 Task Scheduler
.DESCRIPTION
    این اسکریپت یک Task زمان‌بندی‌شده در نشست تعاملی کاربر فعلی (Interactive User Session) می‌سازد تا:
    ۱) بلافاصله پس از روشن شدن و ورود به ویندوز ۱۱، بدون پنجره سیاه ترمینال (با pythonw.exe) اجرا شود.
    ۲) اعلان‌های دسکتاپ ویندوز ۱۱ (Windows Toast Notification) به‌درستی در Action Center نمایش داده شوند.
    ۳) روی لپ‌تاپ در حالت باتری متوقف نشود و محدودیت پیش‌فرض ۷۲ ساعته ویندوز حذف گردد.
#>

$ErrorActionPreference = "Stop"

$TaskName = "SamadFoodMonitor"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonwPath = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (Test-Path $PythonwPath) {
    $Executable = $PythonwPath
} elseif (Test-Path $PythonPath) {
    $Executable = $PythonPath
} else {
    Write-Error "محیط مجازی (.venv) یافت نشد! ابتدا دستورات ساخت .venv و نصب پکیج‌ها را در ریشه پروژه اجرا کنید."
    exit 1
}

$Arguments = "-m samad_monitor run"

# حذف نسخه قبلی Task در صورت وجود
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$Action = New-ScheduledTaskAction `
    -Execute $Executable `
    -Argument $Arguments `
    -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 2)

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "سرویس پایش خودکار انتشار منوی هفته آینده سماد (دانشگاه شهید بهشتی)" | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host "✅ سرویس پس‌زمینه '$TaskName' با موفقیت در Windows Task Scheduler ثبت و اجرا شد!" -ForegroundColor Green
Write-Host "   مسیر پروژه : $ProjectRoot"
Write-Host "   مفسر اجرایی: $Executable"
Write-Host "   برای مشاهده وضعیت، دستور زیر را اجرا کنید:"
Write-Host "   .\.venv\Scripts\python.exe -m samad_monitor status" -ForegroundColor Cyan
