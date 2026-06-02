param(
    [string]$Repo = "",
    [string]$EnvFile = ".env",
    [switch]$IncludeRenderApiKey
)

$ErrorActionPreference = "Stop"

function Read-EnvFile {
    param([string]$Path)

    $result = @{}
    if (-not (Test-Path $Path)) {
        return $result
    }

    foreach ($line in Get-Content -Path $Path) {
        $text = $line.Trim()
        if (-not $text -or $text.StartsWith("#") -or -not $text.Contains("=")) {
            continue
        }
        $idx = $text.IndexOf("=")
        if ($idx -le 0) { continue }
        $key = $text.Substring(0, $idx).Trim()
        $value = $text.Substring($idx + 1).Trim()
        if ($value.StartsWith('"') -and $value.EndsWith('"') -and $value.Length -ge 2) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $result[$key] = $value
    }
    return $result
}

function Resolve-RepoName {
    if (-not [string]::IsNullOrWhiteSpace($Repo)) {
        return $Repo
    }

    $remote = (& git remote get-url origin 2>$null)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($remote)) {
        throw "Cannot resolve GitHub repo. Pass -Repo owner/name."
    }
    $value = [string]$remote.Trim()
    $value = $value -replace "^https://github\.com/", ""
    $value = $value -replace "^git@github\.com:", ""
    $value = $value -replace "\.git$", ""
    if (-not $value.Contains("/")) {
        throw "Cannot parse GitHub repo from origin remote: $remote"
    }
    return $value
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -Path $projectRoot

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh) is not installed. Install it, run 'gh auth login', then rerun this script."
}

$repoName = Resolve-RepoName
$envPath = Join-Path $projectRoot $EnvFile
$envValues = Read-EnvFile -Path $envPath
if ($envValues.Count -eq 0) {
    throw "No env values found in $EnvFile."
}

$prefixes = @(
    "APP_",
    "TOKEN_HEALTHCHECK_",
    "TELEGRAM_",
    "META_",
    "PANCAKE_",
    "REPORT_",
    "DAILY_REPORT_",
    "RECONCILE_COD_",
    "THAI_DUONG_"
)

$exactKeys = @(
    "WEB_REPORT_REFRESH_SECONDS",
    "WEB_REPORT_HOST",
    "WEB_REPORT_PORT",
    "WEB_REPORT_STATUS_MAP_PATH"
)

if ($IncludeRenderApiKey) {
    $exactKeys += "RENDER_API_KEY"
}

$selected = @{}
foreach ($entry in $envValues.GetEnumerator()) {
    $key = [string]$entry.Key
    $value = [string]$entry.Value
    if ([string]::IsNullOrWhiteSpace($value)) {
        continue
    }
    if ($key -eq "RENDER_API_KEY" -and -not $IncludeRenderApiKey) {
        continue
    }
    $include = $false
    if ($exactKeys -contains $key) {
        $include = $true
    } else {
        foreach ($prefix in $prefixes) {
            if ($key.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                $include = $true
                break
            }
        }
    }
    if ($include) {
        $selected[$key] = $value
    }
}

if ($selected.Count -eq 0) {
    throw "No non-empty GitHub Actions secrets selected from $EnvFile."
}

Write-Host ("Uploading {0} GitHub Actions secrets to {1}..." -f $selected.Count, $repoName)
foreach ($key in ($selected.Keys | Sort-Object)) {
    $value = [string]$selected[$key]
    gh secret set $key --repo $repoName --body $value
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to set GitHub secret: $key"
    }
    Write-Host (" - {0}" -f $key)
}

Write-Host "GitHub Actions secrets sync completed."
