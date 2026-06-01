param(
    [string]$EnvFile = ".env"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -Path $projectRoot

$requiredFiles = @(
    "render.yaml",
    "requirements.txt",
    "app/web_report_main.py",
    "config/web_report_status_map.json"
)

$missingFiles = @()
foreach ($file in $requiredFiles) {
    if (-not (Test-Path (Join-Path $projectRoot $file))) {
        $missingFiles += $file
    }
}

$envValues = @{}
$envPath = Join-Path $projectRoot $EnvFile
if (Test-Path $envPath) {
    foreach ($line in Get-Content -Path $envPath) {
        $text = $line.Trim()
        if (-not $text -or $text.StartsWith("#") -or -not $text.Contains("=")) {
            continue
        }
        $idx = $text.IndexOf("=")
        $key = $text.Substring(0, $idx).Trim()
        $value = $text.Substring($idx + 1).Trim()
        $envValues[$key] = $value
    }
}

$requiredEnv = @(
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
    "META_ACCESS_TOKEN",
    "META_AD_ACCOUNT_ID",
    "META_PAGE_ID",
    "PANCAKE_SHOP_ID"
)

$missingEnv = @()
foreach ($key in $requiredEnv) {
    $value = if ($envValues.ContainsKey($key)) { $envValues[$key] } else { "" }
    if ([string]::IsNullOrWhiteSpace($value)) {
        $missingEnv += $key
    }
}

$hasPancakeToken = $false
foreach ($key in @("PANCAKE_ACCESS_TOKEN", "PANCAKE_API_KEY")) {
    $value = if ($envValues.ContainsKey($key)) { $envValues[$key] } else { "" }
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        $hasPancakeToken = $true
        break
    }
}

if (-not $hasPancakeToken) {
    $missingEnv += "PANCAKE_ACCESS_TOKEN or PANCAKE_API_KEY"
}

if ($missingFiles.Count -eq 0 -and $missingEnv.Count -eq 0) {
    Write-Host "Preflight OK. Ready for GitHub push + Render deploy."
    exit 0
}

if ($missingFiles.Count -gt 0) {
    Write-Host "Missing required files:"
    foreach ($item in $missingFiles) {
        Write-Host " - $item"
    }
}

if ($missingEnv.Count -gt 0) {
    Write-Host "Missing required env keys in ${EnvFile}:"
    foreach ($item in $missingEnv) {
        Write-Host " - $item"
    }
}

exit 1
