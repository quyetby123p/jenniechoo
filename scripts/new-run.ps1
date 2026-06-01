param(
    [string]$Prefix = "run"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runsDir = Join-Path $projectRoot "logs/runs"
$locksDir = Join-Path $projectRoot "state/locks"

foreach ($dir in @($runsDir, $locksDir)) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

$safePrefix = $Prefix.ToLowerInvariant() -replace "[^a-z0-9_-]", "-"
$safePrefix = $safePrefix.Trim("-")
if ([string]::IsNullOrWhiteSpace($safePrefix)) {
    $safePrefix = "run"
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$randomPart = -join ((48..57 + 97..122 | Get-Random -Count 4) | ForEach-Object { [char]$_ })
$runId = "{0}_{1}_{2}" -f $safePrefix, $timestamp, $randomPart

$runLogDir = Join-Path $runsDir $runId
New-Item -ItemType Directory -Path $runLogDir -Force | Out-Null

$lockFile = Join-Path $locksDir ($runId + ".lock")
Set-Content -Path $lockFile -Value ("created_at=" + (Get-Date).ToString("o"))

[pscustomobject]@{
    run_id      = $runId
    run_log_dir = $runLogDir
    lock_file   = $lockFile
} | ConvertTo-Json -Depth 3
