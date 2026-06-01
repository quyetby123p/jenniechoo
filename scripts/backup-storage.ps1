param(
    [string]$Label = "manual"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$storageDir = Join-Path $projectRoot "storage"
$backupsDir = Join-Path $storageDir "backups"

if (-not (Test-Path $backupsDir)) {
    New-Item -ItemType Directory -Path $backupsDir -Force | Out-Null
}

$safeLabel = $Label.ToLowerInvariant() -replace "[^a-z0-9_-]", "-"
$safeLabel = $safeLabel.Trim("-")
if ([string]::IsNullOrWhiteSpace($safeLabel)) {
    $safeLabel = "manual"
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$archiveName = "{0}_{1}.zip" -f $safeLabel, $stamp
$archivePath = Join-Path $backupsDir $archiveName

$items = Get-ChildItem -Path $storageDir -Force | Where-Object { $_.Name -ne "backups" }
if ($items.Count -eq 0) {
    throw "Khong co du lieu nao de backup trong storage/."
}

Compress-Archive -Path ($items.FullName) -DestinationPath $archivePath
Write-Output $archivePath
