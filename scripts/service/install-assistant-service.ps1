param(
    [string]$ServiceName = "FBPersonalAssistantBot",
    [string]$ProjectRoot = "",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
        throw "Khong tim thay python.exe. Truyen -PythonExe de chi ro."
    }
    $PythonExe = $pythonCmd.Source
}

$runner = Join-Path $PSScriptRoot "run-assistant-bot.ps1"
if (-not (Test-Path $runner)) {
    throw "Khong tim thay script run-assistant-bot.ps1"
}

$binPath = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runner`" -ProjectRoot `"$ProjectRoot`" -PythonExe `"$PythonExe`""

sc.exe create $ServiceName binPath= "$binPath" start= auto DisplayName= "$ServiceName" | Out-Null
sc.exe description $ServiceName "Telegram personal assistant bot" | Out-Null

Write-Output "Da cai service $ServiceName"
Write-Output "Dung scripts/service/start-service.ps1 -ServiceName $ServiceName de chay service."
