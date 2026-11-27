param(
    [string]$EnvPath = ".env"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $EnvPath)) {
    throw "Khong tim thay file env: $EnvPath"
}

$required = @(
    "WEB_REPORT_API_TOKEN",
    "META_ACCESS_TOKEN",
    "META_AD_ACCOUNT_ID",
    "PANCAKE_ACCESS_TOKEN",
    "PANCAKE_SHOP_ID",
    "ADS2_META_ACCESS_TOKEN",
    "ADS2_META_AD_ACCOUNT_ID",
    "ADS2_PANCAKE_ACCESS_TOKEN",
    "ADS2_PANCAKE_SHOP_ID"
)
$values = @{}
foreach ($line in Get-Content -LiteralPath $EnvPath) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
        $values[$Matches[1]] = $Matches[2].Trim().Trim('"')
    }
}

$missing = @()
foreach ($key in $required) {
    $value = [string]$values[$key]
    if ([string]::IsNullOrWhiteSpace($value) -or $value -match '^<.*>$' -or $value -eq 'replace_me') {
        $missing += $key
    }
}
if ($missing.Count -gt 0) {
    throw ("Thieu credential/config bat buoc: " + ($missing -join ', '))
}

if ([string]$values["PANCAKE_SHOP_ID"] -ne "407330767") {
    throw "PANCAKE_SHOP_ID phai la shop JC 407330767."
}
if ([string]$values["ADS2_PANCAKE_SHOP_ID"] -ne "408116196") {
    throw "ADS2_PANCAKE_SHOP_ID phai la shop Ruza Thailand 408116196."
}
Write-Output "Web report env check: OK"
