<#
.SYNOPSIS
    حذف اجرای خودکار Samad Food Monitor از Startup و Task Scheduler ویندوز ۱۱
#>

$ErrorActionPreference = "SilentlyContinue"
$TaskName = "SamadFoodMonitor"
$StartupDir = [Environment]::GetFolderPath("Startup")
$VbsPath = Join-Path $StartupDir "SamadFoodMonitor_AutoStart.vbs"

$RemovedAny = $false

if (Test-Path $VbsPath) {
    Remove-Item -Path $VbsPath -Force
    $RemovedAny = $true
}

if (Get-ScheduledTask -TaskName $TaskName) {
    Stop-ScheduledTask -TaskName $TaskName
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    $RemovedAny = $true
}

if ($RemovedAny) {
    Write-Host "✅ اجرای خودکار Samad Food Monitor از استارتاپ ویندوز حذف شد." -ForegroundColor Green
} else {
    Write-Host "ℹ️ موردی در استارتاپ ویندوز یافت نشد." -ForegroundColor Yellow
}
