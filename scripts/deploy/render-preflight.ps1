param(
    [string]$EnvFile = ".env",
    [ValidateSet("web", "stack")]
    [string]$Scope = "web"
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
        $result[$key] = $value
    }
    return $result
}

function Has-EnvValue {
    param(
        [hashtable]$EnvMap,
        [string]$Key
    )
    if (-not $EnvMap.ContainsKey($Key)) {
        return $false
    }
    return -not [string]::IsNullOrWhiteSpace([string]$EnvMap[$Key])
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -Path $projectRoot

$requiredFiles = @(
    "render.yaml",
    "requirements.txt",
    "app/web_report_main.py",
    "config/web_report_status_map.json"
)

if ($Scope -eq "stack") {
    $requiredFiles += @(
        "app/main.py",
        "app/media_main.py",
        "app/assistant_main.py",
        "scripts/deploy/render-sync-stack.ps1"
    )
}

$missingFiles = @()
foreach ($file in $requiredFiles) {
    if (-not (Test-Path (Join-Path $projectRoot $file))) {
        $missingFiles += $file
    }
}

$envPath = Join-Path $projectRoot $EnvFile
$envValues = Read-EnvFile -Path $envPath

$requiredEnv = @(
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
    "META_ACCESS_TOKEN",
    "META_AD_ACCOUNT_ID",
    "META_PAGE_ID",
    "PANCAKE_SHOP_ID"
)

if ($Scope -eq "stack") {
    $requiredEnv += @(
        "MEDIA_BOT_TELEGRAM_TOKEN",
        "MEDIA_BOT_ALLOWED_USER_ID",
        "BOT3_TELEGRAM_TOKEN",
        "BOT3_ALLOWED_USER_ID",
        "BOT3_GOOGLE_OAUTH_CLIENT_ID",
        "BOT3_GOOGLE_OAUTH_CLIENT_SECRET",
        "BOT3_GOOGLE_OAUTH_REFRESH_TOKEN"
    )
}

$missingEnv = @()
foreach ($key in $requiredEnv) {
    if (-not (Has-EnvValue -EnvMap $envValues -Key $key)) {
        $missingEnv += $key
    }
}

$hasPancakeCredential = $false
foreach ($key in @("PANCAKE_ACCESS_TOKEN", "PANCAKE_API_KEY")) {
    if (Has-EnvValue -EnvMap $envValues -Key $key) {
        $hasPancakeCredential = $true
        break
    }
}
if (-not $hasPancakeCredential) {
    $missingEnv += "PANCAKE_ACCESS_TOKEN or PANCAKE_API_KEY"
}

$configErrors = @()
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $pythonExe = [string]$pythonCommand.Source
    }
}

if ((Test-Path $pythonExe) -or (Get-Command $pythonExe -ErrorAction SilentlyContinue)) {
    $commands = New-Object System.Collections.Generic.List[object]
    $commands.Add(@("-m", "app.main", "--check-config"))
    if ($Scope -eq "stack") {
        $commands.Add(@("-m", "app.media_main", "--check-config"))
        $commands.Add(@("-m", "app.assistant_main", "--check-config"))
        $commands.Add(@("-m", "app.work_progress_main", "--check-config"))
    }

    foreach ($commandArgs in $commands) {
        $argText = $commandArgs -join " "
        & cmd /c "`"$pythonExe`" $argText" >$null 2>&1
        if ($LASTEXITCODE -ne 0) {
            $configErrors += ("python " + ($commandArgs -join " "))
        }
    }
}

if ($missingFiles.Count -eq 0 -and $missingEnv.Count -eq 0 -and $configErrors.Count -eq 0) {
    Write-Host ("Preflight OK ({0}). Ready for GitHub push + Render deploy." -f $Scope)
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

if ($configErrors.Count -gt 0) {
    Write-Host "Config check commands failed:"
    foreach ($item in $configErrors) {
        Write-Host " - $item"
    }
}

exit 1
