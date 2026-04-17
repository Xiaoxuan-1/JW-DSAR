param(
    [string]$TaskPrefix = "JWDSAR",
    [string]$DailyTime = "23:50",
    [string]$PythonBin = $env:JWDSAR_PYTHON_BIN,
    [string]$OutputDir = "",
    [switch]$InstallWebTask
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($PythonBin)) {
    $PythonBin = "python"
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$dailyScript = Join-Path $repoRoot "scripts\run_daily_report_windows.ps1"
$logDir = Join-Path $repoRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$taskDaily = "$TaskPrefix-DailyReport"
$taskWeb = "$TaskPrefix-Web"

Write-Host "Repo root: $repoRoot"
Write-Host "Python bin: $PythonBin"
Write-Host "Daily time: $DailyTime"

if (-not (Test-Path $dailyScript)) {
    throw "Missing script: $dailyScript"
}

# Daily generate-once task
$dailyArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$dailyScript`""
)
if (-not [string]::IsNullOrWhiteSpace($OutputDir)) {
    $dailyArgs += @("-OutputDir", "`"$OutputDir`"")
}
if (-not [string]::IsNullOrWhiteSpace($PythonBin)) {
    $dailyArgs += @("-PythonBin", "`"$PythonBin`"")
}

$actionDaily = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($dailyArgs -join " ")
$triggerDaily = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($DailyTime, "HH:mm", $null))
$settingsDaily = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

try {
    Unregister-ScheduledTask -TaskName $taskDaily -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
} catch {}
Register-ScheduledTask -TaskName $taskDaily -Action $actionDaily -Trigger $triggerDaily -Settings $settingsDaily -RunLevel Highest | Out-Null
Write-Host "Installed scheduled task: $taskDaily"

# Optional: Web task at logon (best-effort; task scheduler restarts can be set later)
if ($InstallWebTask) {
    $webArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-Command",
        # Run in repoRoot so relative paths work
        "cd `"$repoRoot`"; `$env:PORT=$env:JWDSAR_PORT; & `"$PythonBin`" app_scheduled.py *> `"$logDir\jwdsar-web.log`""
    )
    $actionWeb = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($webArgs -join " ")
    $triggerWeb = New-ScheduledTaskTrigger -AtLogOn
    $settingsWeb = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

    try {
        Unregister-ScheduledTask -TaskName $taskWeb -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    } catch {}
    Register-ScheduledTask -TaskName $taskWeb -Action $actionWeb -Trigger $triggerWeb -Settings $settingsWeb -RunLevel Highest | Out-Null
    Write-Host "Installed scheduled task: $taskWeb"
    Write-Host "Tip: to start now, run: schtasks /Run /TN `"$taskWeb`""
}

Write-Host "Done."

