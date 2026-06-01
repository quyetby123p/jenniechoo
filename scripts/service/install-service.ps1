param(
    [string]$ServiceName = "FBAdsTelegramBot",
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

$runner = Join-Path $PSScriptRoot "run-bot.ps1"
if (-not (Test-Path $runner)) {
    throw "Khong tim thay script run-bot.ps1"
}

$binPath = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runner`" -ProjectRoot `"$ProjectRoot`" -PythonExe `"$PythonExe`""

$createOutput = sc.exe create $ServiceName binPath= "$binPath" start= auto DisplayName= "$ServiceName" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Cai service that bai: $createOutput"
}

$descOutput = sc.exe description $ServiceName "Telegram to FB Ads automation bot" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Dat mo ta service that bai: $descOutput"
}

if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
    throw "Khong tim thay service $ServiceName sau khi cai dat."
}

Write-Output "Da cai service $ServiceName"
Write-Output "Dung lenh start-service.ps1 de chay service."
