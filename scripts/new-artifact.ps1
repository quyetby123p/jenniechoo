param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("campaign", "creative", "audience", "report", "import", "export", "backup", "archive", "tmp")]
    [string]$Type,

    [string]$Name = "artifact",
    [string]$Ext = "json"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$typeMap = @{
    campaign = "campaigns"
    creative = "creatives"
    audience = "audiences"
    report   = "reports"
    import   = "imports"
    export   = "exports"
    backup   = "backups"
    archive  = "archive"
    tmp      = "tmp"
}

$targetDir = Join-Path $projectRoot ("storage/" + $typeMap[$Type])
if (-not (Test-Path $targetDir)) {
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
}

$safeName = $Name.ToLowerInvariant() -replace "[^a-z0-9_-]", "-"
$safeName = $safeName.Trim("-")
if ([string]::IsNullOrWhiteSpace($safeName)) {
    $safeName = "artifact"
}

$safeExt = $Ext.Trim().TrimStart(".").ToLowerInvariant()
if ([string]::IsNullOrWhiteSpace($safeExt)) {
    $safeExt = "json"
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
do {
    $randomPart = -join ((48..57 + 97..122 | Get-Random -Count 6) | ForEach-Object { [char]$_ })
    $fileName = "{0}_{1}_{2}.{3}" -f $safeName, $timestamp, $randomPart, $safeExt
    $fullPath = Join-Path $targetDir $fileName
} while (Test-Path $fullPath)

New-Item -ItemType File -Path $fullPath | Out-Null
Write-Output $fullPath
