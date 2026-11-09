<#
.SYNOPSIS
    توقف و حذف کامل Task خودکار Samad Food Monitor از Windows Task Scheduler
#>

$ErrorActionPreference = "SilentlyContinue"
$TaskName = "SamadFoodMonitor"

if (Get-ScheduledTask -TaskName $TaskName) {
    Stop-ScheduledTask -TaskName $TaskName
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "✅ سرویس '$TaskName' با موفقیت متوقف و از Windows Task Scheduler حذف شد." -ForegroundColor Green
} else {
    Write-Host "ℹ️ سرویسی با نام '$TaskName' در Task Scheduler یافت نشد." -ForegroundColor Yellow
}
