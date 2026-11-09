<#
.SYNOPSIS
    فعال‌سازی اجرای خودکار دائمی Samad Food Monitor به محض روشن شدن لپ‌تاپ در Windows 11
.DESCRIPTION
    این اسکریپت بدون نیاز به دسترسی Administrator، یک راه‌انداز مخفی (pythonw.exe بدون پنجره سیاه)
    در پوشه Startup ویندوز کاربر فعلی می‌سازد (و در صورت داشتن دسترسی Admin، در Task Scheduler هم ثبت می‌کند)
    تا هر بار لپ‌تاپ روشن شود، هم پایش خودکار سماد و هم ربات تلگرام بلافاصله فعال شوند.
#>

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonwPath = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (Test-Path $PythonwPath) {
    $Executable = $PythonwPath
} elseif (Test-Path $PythonPath) {
    $Executable = $PythonPath
} else {
    Write-Error "محیط مجازی (.venv) یافت نشد! ابتدا در پوشه پروژه محیط مجازی را فعال کنید."
    exit 1
}

$StartupDir = [Environment]::GetFolderPath("Startup")
$VbsPath = Join-Path $StartupDir "SamadFoodMonitor_AutoStart.vbs"

$SafeRoot = $ProjectRoot.Replace('"', '""')
$SafeExe = $Executable.Replace('"', '""')

$VbsLines = @(
    'Set WshShell = CreateObject("WScript.Shell")',
    "WshShell.CurrentDirectory = `"$SafeRoot`"",
    "WshShell.Run `"`"`"$SafeExe`"`" -m samad_monitor ui --port 8765 --no-browser --auto-start`", 0, False"
) -join "`r`n"

[System.IO.File]::WriteAllText($VbsPath, $VbsLines, [System.Text.Encoding]::Unicode)

# اجرای فوری در پس‌زمینه همین الان (اگر در حال اجرا نباشد)
Start-Process -FilePath "wscript.exe" -ArgumentList "`"$VbsPath`""

Write-Host "✅ اجرای خودکار دائمی با روشن شدن لپ‌تاپ فعال شد!" -ForegroundColor Green
Write-Host "   فایل استارتاپ : $VbsPath"
Write-Host "   پنل مدیریت وب: http://127.0.0.1:8765" -ForegroundColor Cyan
