param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [string]$PythonExe = "",
    [int]$CheckIntervalSeconds = 20
)

$ErrorActionPreference = "Stop"

if ($CheckIntervalSeconds -lt 5) {
    $CheckIntervalSeconds = 5
}

$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $candidate = Join-Path $resolvedProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $candidate) {
        $PythonExe = $candidate
    } else {
        $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCmd) {
            throw "Khong tim thay python.exe. Truyen -PythonExe de chi ro."
        }
        $PythonExe = $pythonCmd.Source
    }
}

$logsDir = Join-Path $resolvedProjectRoot "logs\app"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
$watchdogLog = Join-Path $logsDir "watchdog.log"

function Write-WatchdogLog {
    param([string]$Message)
    $timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Add-Content -Path $watchdogLog -Value "$timestamp [WATCHDOG] $Message"
}

function Get-PythonModuleProcesses {
    param([Parameter(Mandatory = $true)][string]$ModuleName)
    $escapedRoot = [Regex]::Escape($resolvedProjectRoot)
    $escapedModule = [Regex]::Escape($ModuleName)
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.CommandLine -match "-m $escapedModule" -and $_.CommandLine -match $escapedRoot
    }
}

function Ensure-PythonModuleRunning {
    param([Parameter(Mandatory = $true)][string]$ModuleName)

    $running = @(Get-PythonModuleProcesses -ModuleName $ModuleName)
    if ($running.Count -eq 0) {
        Write-WatchdogLog "Khong tim thay $ModuleName, bat dau khoi dong lai."
        Start-Process -FilePath $PythonExe -ArgumentList "-m $ModuleName" -WorkingDirectory $resolvedProjectRoot -WindowStyle Hidden | Out-Null
        Start-Sleep -Seconds 3
        $afterStart = @(Get-PythonModuleProcesses -ModuleName $ModuleName)
        if ($afterStart.Count -gt 0) {
            $pids = ($afterStart | Select-Object -ExpandProperty ProcessId) -join ","
            Write-WatchdogLog "Khoi dong lai $ModuleName thanh cong. PIDs=$pids"
        } else {
            Write-WatchdogLog "Khoi dong lai $ModuleName that bai: khong tim thay process sau khi start."
        }
    }
}

$mutexName = "Global\FBAdsAutomationMainWatchdog"
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$hasLock = $false

try {
    $hasLock = $mutex.WaitOne(0, $false)
    if (-not $hasLock) {
        Write-WatchdogLog "Watchdog khac dang chay, thoat."
        exit 0
    }

    Set-Location -Path $resolvedProjectRoot
    Write-WatchdogLog "Watchdog bat dau. ProjectRoot=$resolvedProjectRoot"

    while ($true) {
        try {
            Ensure-PythonModuleRunning -ModuleName "app.main"
            Ensure-PythonModuleRunning -ModuleName "app.assistant_main"
        } catch {
            Write-WatchdogLog ("Loi watchdog loop: " + $_.Exception.Message)
        }
        Start-Sleep -Seconds $CheckIntervalSeconds
    }
} finally {
    if ($hasLock) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
