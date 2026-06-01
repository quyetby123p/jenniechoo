param(
    [string]$RenderApiKey = "",
    [string]$OwnerId = "",
    [string]$ServiceName = "fb-ops-web-report",
    [string]$RepoUrl = "https://github.com/quyetby123p/jenniechoo",
    [string]$Branch = "main",
    [string]$RootDir = "",
    [string]$Plan = "free",
    [string]$Region = "oregon",
    [string]$EnvFile = ".env",
    [switch]$SkipDeployTrigger
)

$ErrorActionPreference = "Stop"

function Invoke-RenderApi {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("GET", "POST", "PUT", "PATCH", "DELETE")]
        [string]$Method,
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [hashtable]$Query = @{},
        [object]$Body = $null
    )

    $baseUrl = "https://api.render.com/v1"
    $pairs = @()
    foreach ($entry in $Query.GetEnumerator()) {
        if ($null -eq $entry.Value) { continue }
        $valueText = [string]$entry.Value
        if ([string]::IsNullOrWhiteSpace($valueText)) { continue }
        $pairs += ("{0}={1}" -f [System.Uri]::EscapeDataString([string]$entry.Key), [System.Uri]::EscapeDataString($valueText))
    }
    $queryText = if ($pairs.Count -gt 0) { "?" + ($pairs -join "&") } else { "" }
    $uri = "$baseUrl$Path$queryText"

    $headers = @{
        "Authorization" = "Bearer $RenderApiKey"
        "Accept" = "application/json"
    }

    if ($null -eq $Body) {
        return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers
    }

    $bodyJson = $Body | ConvertTo-Json -Depth 20 -Compress
    return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers -ContentType "application/json" -Body $bodyJson
}

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

function Get-EnvValue {
    param(
        [hashtable]$EnvMap,
        [string]$Key,
        [string]$Default = ""
    )

    if ($EnvMap.ContainsKey($Key) -and -not [string]::IsNullOrWhiteSpace([string]$EnvMap[$Key])) {
        return [string]$EnvMap[$Key]
    }
    return $Default
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -Path $projectRoot

if ([string]::IsNullOrWhiteSpace($RenderApiKey)) {
    $RenderApiKey = [string]$env:RENDER_API_KEY
}
if ([string]::IsNullOrWhiteSpace($RenderApiKey)) {
    throw "Missing Render API key. Pass -RenderApiKey or set RENDER_API_KEY in your shell."
}

$envPath = Join-Path $projectRoot $EnvFile
$envMap = Read-EnvFile -Path $envPath

$requiredEnv = @(
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
    "META_ACCESS_TOKEN",
    "META_AD_ACCOUNT_ID",
    "META_PAGE_ID",
    "PANCAKE_SHOP_ID"
)

$missing = @()
foreach ($key in $requiredEnv) {
    $value = Get-EnvValue -EnvMap $envMap -Key $key
    if ([string]::IsNullOrWhiteSpace($value)) {
        $missing += $key
    }
}

$hasPancakeCredential = $false
foreach ($k in @("PANCAKE_ACCESS_TOKEN", "PANCAKE_API_KEY")) {
    $value = Get-EnvValue -EnvMap $envMap -Key $k
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        $hasPancakeCredential = $true
        break
    }
}
if (-not $hasPancakeCredential) {
    $missing += "PANCAKE_ACCESS_TOKEN or PANCAKE_API_KEY"
}

if ($missing.Count -gt 0) {
    throw "Missing required keys in ${EnvFile}: $($missing -join ', ')"
}

$owners = @(Invoke-RenderApi -Method "GET" -Path "/owners")
if ($owners.Count -eq 0) {
    throw "No Render workspace found for this API key."
}

if ([string]::IsNullOrWhiteSpace($OwnerId)) {
    $OwnerId = [string]$owners[0].owner.id
}
if ([string]::IsNullOrWhiteSpace($OwnerId)) {
    throw "Cannot resolve ownerId. Pass -OwnerId explicitly."
}

$ownerMatch = $owners | Where-Object { [string]$_.owner.id -eq $OwnerId }
if (-not $ownerMatch) {
    throw "ownerId '$OwnerId' is not accessible with this API key."
}

$envVars = @()

$baseEnvKeys = @(
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
    "META_ACCESS_TOKEN",
    "META_AD_ACCOUNT_ID",
    "META_PAGE_ID",
    "PANCAKE_SHOP_ID"
)

foreach ($key in $baseEnvKeys) {
    $envVars += @{ key = $key; value = (Get-EnvValue -EnvMap $envMap -Key $key) }
}

foreach ($optionalKey in @("META_PAGE_ACCESS_TOKEN", "PANCAKE_ACCESS_TOKEN", "PANCAKE_API_KEY")) {
    $value = Get-EnvValue -EnvMap $envMap -Key $optionalKey
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        $envVars += @{ key = $optionalKey; value = $value }
    }
}

$envVars += @{ key = "APP_TIMEZONE"; value = (Get-EnvValue -EnvMap $envMap -Key "APP_TIMEZONE" -Default "Asia/Ho_Chi_Minh") }
$envVars += @{ key = "WEB_REPORT_REFRESH_SECONDS"; value = (Get-EnvValue -EnvMap $envMap -Key "WEB_REPORT_REFRESH_SECONDS" -Default "600") }
$envVars += @{ key = "WEB_REPORT_HOST"; value = (Get-EnvValue -EnvMap $envMap -Key "WEB_REPORT_HOST" -Default "0.0.0.0") }

foreach ($optionalKey in @("PANCAKE_API_BASE_URL", "PANCAKE_PAGE_SIZE", "REPORT_THB_TO_VND_RATE", "REPORT_THB_MINOR_UNIT_FACTOR")) {
    $value = Get-EnvValue -EnvMap $envMap -Key $optionalKey
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        $envVars += @{ key = $optionalKey; value = $value }
    }
}

$existingRaw = @(Invoke-RenderApi -Method "GET" -Path "/services" -Query @{ ownerId = $OwnerId; name = $ServiceName; type = "web_service"; limit = "100" })
$existingService = $null
foreach ($item in $existingRaw) {
    $service = if ($item.service) { $item.service } else { $item }
    if ($service -and [string]$service.name -eq $ServiceName -and [string]$service.type -eq "web_service") {
        $existingService = $service
        break
    }
}

if ($null -eq $existingService) {
    $createPayload = @{
        type = "web_service"
        name = $ServiceName
        ownerId = $OwnerId
        repo = $RepoUrl
        autoDeploy = "yes"
        branch = $Branch
        envVars = $envVars
        serviceDetails = @{
            runtime = "python"
            region = $Region
            plan = $Plan
            healthCheckPath = "/healthz"
            envSpecificDetails = @{
                buildCommand = "pip install -r requirements.txt"
                startCommand = 'gunicorn app.web_report_main:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120'
            }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($RootDir)) {
        $createPayload["rootDir"] = $RootDir
    }

    $created = Invoke-RenderApi -Method "POST" -Path "/services" -Body $createPayload
    $service = $created.service
    $deployId = [string]$created.deployId

    Write-Host "Created Render service successfully."
    Write-Host ("Service ID: {0}" -f [string]$service.id)
    Write-Host ("Dashboard: {0}" -f [string]$service.dashboardUrl)
    if (-not [string]::IsNullOrWhiteSpace($deployId)) {
        Write-Host ("Initial deploy ID: {0}" -f $deployId)
    }
    exit 0
}

$serviceId = [string]$existingService.id

$updatePayload = @{
    autoDeploy = "yes"
    repo = $RepoUrl
    branch = $Branch
    serviceDetails = @{
        runtime = "python"
        region = $Region
        plan = $Plan
        healthCheckPath = "/healthz"
        envSpecificDetails = @{
            buildCommand = "pip install -r requirements.txt"
            startCommand = 'gunicorn app.web_report_main:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120'
        }
    }
}
if (-not [string]::IsNullOrWhiteSpace($RootDir)) {
    $updatePayload["rootDir"] = $RootDir
}

$updatedService = Invoke-RenderApi -Method "PATCH" -Path "/services/$serviceId" -Body $updatePayload
$null = Invoke-RenderApi -Method "PUT" -Path "/services/$serviceId/env-vars" -Body $envVars

Write-Host "Updated existing Render service successfully."
Write-Host ("Service ID: {0}" -f $serviceId)
Write-Host ("Dashboard: {0}" -f [string]$updatedService.dashboardUrl)

if (-not $SkipDeployTrigger) {
    $deploy = Invoke-RenderApi -Method "POST" -Path "/services/$serviceId/deploys" -Body @{}
    if ($deploy -and $deploy.deploy) {
        Write-Host ("Triggered deploy ID: {0}" -f [string]$deploy.deploy.id)
    } else {
        Write-Host "Triggered deploy."
    }
}
