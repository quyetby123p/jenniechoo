param(
    [string]$ProjectRoot = "",
    [string]$PythonExe = "",
    [int]$CheckIntervalSeconds = 20,
    [string]$StartupFileName = "start-fb-ads-watchdog.cmd"
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

$startupDir = [Environment]::GetFolderPath("Startup")
if ([string]::IsNullOrWhiteSpace($startupDir) -or -not (Test-Path -LiteralPath $startupDir)) {
    throw "Khong tim thay thu muc Startup cua user."
}

$cmdPath = Join-Path $startupDir $StartupFileName
$cmdContent = @"
@echo off
cd /d "$ProjectRoot"
start "FBAdsWatchdog" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$watchdogScript" -ProjectRoot "$ProjectRoot" -PythonExe "$PythonExe" -CheckIntervalSeconds $CheckIntervalSeconds
"@

Set-Content -LiteralPath $cmdPath -Value $cmdContent -Encoding ASCII

Write-Output "Da cai startup watchdog:"
Write-Output $cmdPath
