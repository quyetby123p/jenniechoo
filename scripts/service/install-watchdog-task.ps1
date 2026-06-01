param(
    [string]$TaskName = "FBAdsMainWatchdog",
    [string]$ProjectRoot = "",
    [string]$PythonExe = "",
    [int]$CheckIntervalSeconds = 20
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        $PythonExe = $venvPython
    } else {
        $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCmd) {
            throw "Khong tim thay python.exe. Truyen -PythonExe de chi ro."
        }
        $PythonExe = $pythonCmd.Source
    }
}

$watchdogScript = Join-Path $PSScriptRoot "run-bot-watchdog.ps1"
if (-not (Test-Path -LiteralPath $watchdogScript)) {
    throw "Khong tim thay script watchdog: $watchdogScript"
}

$currentUser = "$env:USERDOMAIN\$env:USERNAME"
$actionArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-WindowStyle", "Hidden",
    "-File", "`"$watchdogScript`"",
    "-ProjectRoot", "`"$ProjectRoot`"",
    "-PythonExe", "`"$PythonExe`"",
    "-CheckIntervalSeconds", "$CheckIntervalSeconds"
) -join " "

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArgs
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $triggerLogon `
        -Settings $settings `
        -Principal $principal `
        -Description "Watchdog giu app.main luon hoat dong" `
        -Force | Out-Null
} catch {
    Write-Warning "Khong cai duoc Scheduled Task (co the thieu quyen): $($_.Exception.Message)"
    Write-Warning "Fallback: dung script install-startup-watchdog.ps1 de cai auto-start theo user startup."
    throw
}

try {
    $triggerStartup = New-ScheduledTaskTrigger -AtStartup
    Set-ScheduledTask -TaskName $TaskName -Trigger @($triggerLogon, $triggerStartup) | Out-Null
} catch {
    Write-Warning "Khong them duoc trigger AtStartup (co the thieu quyen admin). Van da cai trigger AtLogOn."
}

Start-ScheduledTask -TaskName $TaskName

Write-Output "Da cai va kick task watchdog: $TaskName"
Write-Output "ProjectRoot: $ProjectRoot"
Write-Output "PythonExe: $PythonExe"
