param(
    [string]$TaskName = "FBAdsMainWatchdog"
)

$ErrorActionPreference = "Stop"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Da go task watchdog: $TaskName"
} else {
    Write-Output "Khong tim thay task watchdog: $TaskName"
}
