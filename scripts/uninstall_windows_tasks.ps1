param(
    [string]$TaskPrefix = "JWDSAR"
)

$ErrorActionPreference = "Stop"

$taskDaily = "$TaskPrefix-DailyReport"
$taskWeb = "$TaskPrefix-Web"

try {
    Unregister-ScheduledTask -TaskName $taskDaily -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Write-Host "Removed scheduled task: $taskDaily"
} catch {}

try {
    Unregister-ScheduledTask -TaskName $taskWeb -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Write-Host "Removed scheduled task: $taskWeb"
} catch {}

Write-Host "Done."

