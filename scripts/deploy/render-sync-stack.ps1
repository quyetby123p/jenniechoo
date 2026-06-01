param(
    [string]$RenderApiKey = "",
    [string]$OwnerId = "",
    [string]$RepoUrl = "https://github.com/quyetby123p/jenniechoo",
    [string]$Branch = "main",
    [string]$RootDir = "",
    [string]$Region = "oregon",
    [string]$EnvFile = ".env",
    [string]$WebPlan = "free",
    [string]$WorkerPlan = "starter",
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

    $bodyJson = $Body | ConvertTo-Json -Depth 30 -Compress
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

function Merge-EnvMaps {
    param(
        [hashtable]$FileEnv
    )

    $merged = @{}
    foreach ($item in Get-ChildItem Env:) {
        $merged[$item.Name] = [string]$item.Value
    }
    foreach ($key in $FileEnv.Keys) {
        $merged[$key] = [string]$FileEnv[$key]
    }
    return $merged
}

function Get-EnvValue {
    param(
        [hashtable]$EnvMap,
        [string]$Key,
        [string]$Default = ""
    )

    if ($EnvMap.ContainsKey($Key)) {
        $value = [string]$EnvMap[$Key]
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            return $value
        }
    }
    return $Default
}

function Add-KeyIfValue {
    param(
        [System.Collections.Generic.HashSet[string]]$Set,
        [string]$Key,
        [hashtable]$EnvMap
    )

    if ([string]::IsNullOrWhiteSpace($Key)) {
        return
    }
    $value = Get-EnvValue -EnvMap $EnvMap -Key $Key
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        $null = $Set.Add($Key)
    }
}

function Build-EnvVarList {
    param(
        [hashtable]$EnvMap,
        [string[]]$ExactKeys,
        [string[]]$Prefixes,
        [hashtable]$DefaultValues = @{}
    )

    $selected = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)

    foreach ($key in $ExactKeys) {
        Add-KeyIfValue -Set $selected -Key $key -EnvMap $EnvMap
    }

    foreach ($key in $EnvMap.Keys) {
        foreach ($prefix in $Prefixes) {
            if ($key.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                Add-KeyIfValue -Set $selected -Key $key -EnvMap $EnvMap
                break
            }
        }
    }

    foreach ($key in $DefaultValues.Keys) {
        $value = Get-EnvValue -EnvMap $EnvMap -Key $key -Default ([string]$DefaultValues[$key])
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            $null = $selected.Add($key)
            $EnvMap[$key] = $value
        }
    }

    $keys = @($selected.ToArray() | Sort-Object)
    $envVars = @()
    foreach ($key in $keys) {
        $value = Get-EnvValue -EnvMap $EnvMap -Key $key
        if ([string]::IsNullOrWhiteSpace($value)) {
            continue
        }
        $envVars += @{ key = $key; value = $value }
    }
    return $envVars
}

function Validate-RequiredEnv {
    param(
        [hashtable]$EnvMap,
        [string]$ProfileName,
        [string[]]$RequiredAll,
        [array]$RequiredAnyGroups
    )

    $missing = @()
    foreach ($key in $RequiredAll) {
        $value = Get-EnvValue -EnvMap $EnvMap -Key $key
        if ([string]::IsNullOrWhiteSpace($value)) {
            $missing += $key
        }
    }

    foreach ($group in $RequiredAnyGroups) {
        $found = $false
        foreach ($key in $group) {
            $value = Get-EnvValue -EnvMap $EnvMap -Key $key
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                $found = $true
                break
            }
        }
        if (-not $found) {
            $missing += ($group -join " or ")
        }
    }

    if ($missing.Count -gt 0) {
        throw "Missing required env for profile '$ProfileName': $($missing -join ', ')"
    }
}

function Find-RenderService {
    param(
        [string]$OwnerId,
        [string]$ServiceName,
        [string]$ServiceType
    )

    $existingRaw = @(Invoke-RenderApi -Method "GET" -Path "/services" -Query @{ ownerId = $OwnerId; name = $ServiceName; type = $ServiceType; limit = "100" })
    foreach ($item in $existingRaw) {
        $service = if ($item.service) { $item.service } else { $item }
        if ($service -and [string]$service.name -eq $ServiceName -and [string]$service.type -eq $ServiceType) {
            return $service
        }
    }
    return $null
}

function Upsert-RenderService {
    param(
        [hashtable]$Profile,
        [string]$OwnerId,
        [string]$RepoUrl,
        [string]$Branch,
        [string]$RootDir,
        [string]$Region,
        [string]$Plan,
        [array]$EnvVars,
        [switch]$SkipDeployTrigger
    )

    $serviceType = [string]$Profile.type
    $serviceName = [string]$Profile.name
    $runtime = [string]$Profile.runtime
    $startCommand = [string]$Profile.startCommand
    $buildCommand = [string]$Profile.buildCommand
    $healthCheckPath = [string]$Profile.healthCheckPath

    $serviceDetails = @{
        runtime = $runtime
        region = $Region
        plan = $Plan
        envSpecificDetails = @{
            buildCommand = $buildCommand
            startCommand = $startCommand
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($healthCheckPath) -and $serviceType -eq "web_service") {
        $serviceDetails["healthCheckPath"] = $healthCheckPath
    }

    $existingService = Find-RenderService -OwnerId $OwnerId -ServiceName $serviceName -ServiceType $serviceType
    if ($null -eq $existingService) {
        $createPayload = @{
            type = $serviceType
            name = $serviceName
            ownerId = $OwnerId
            repo = $RepoUrl
            autoDeploy = "yes"
            branch = $Branch
            envVars = $EnvVars
            serviceDetails = $serviceDetails
        }
        if (-not [string]::IsNullOrWhiteSpace($RootDir)) {
            $createPayload["rootDir"] = $RootDir
        }

        $created = Invoke-RenderApi -Method "POST" -Path "/services" -Body $createPayload
        $service = $created.service
        $deployId = [string]$created.deployId

        Write-Host ("Created {0} ({1})" -f $serviceName, $serviceType)
        Write-Host (" - Service ID: {0}" -f [string]$service.id)
        Write-Host (" - Dashboard: {0}" -f [string]$service.dashboardUrl)
        if (-not [string]::IsNullOrWhiteSpace($deployId)) {
            Write-Host (" - Initial deploy: {0}" -f $deployId)
        }
        return
    }

    $serviceId = [string]$existingService.id
    $updatePayload = @{
        autoDeploy = "yes"
        repo = $RepoUrl
        branch = $Branch
        serviceDetails = $serviceDetails
    }
    if (-not [string]::IsNullOrWhiteSpace($RootDir)) {
        $updatePayload["rootDir"] = $RootDir
    }

    $updated = Invoke-RenderApi -Method "PATCH" -Path "/services/$serviceId" -Body $updatePayload
    $null = Invoke-RenderApi -Method "PUT" -Path "/services/$serviceId/env-vars" -Body $EnvVars

    Write-Host ("Updated {0} ({1})" -f $serviceName, $serviceType)
    Write-Host (" - Service ID: {0}" -f $serviceId)
    Write-Host (" - Dashboard: {0}" -f [string]$updated.dashboardUrl)

    if (-not $SkipDeployTrigger) {
        $deploy = Invoke-RenderApi -Method "POST" -Path "/services/$serviceId/deploys" -Body @{}
        if ($deploy -and $deploy.deploy) {
            Write-Host (" - Triggered deploy: {0}" -f [string]$deploy.deploy.id)
        } else {
            Write-Host " - Triggered deploy."
        }
    }
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
$fileEnv = Read-EnvFile -Path $envPath
$envMap = Merge-EnvMaps -FileEnv $fileEnv

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

$commonBuildCommand = "pip install -r requirements.txt"
$profiles = @(
    @{
        name = "fb-ops-web-report"
        type = "web_service"
        runtime = "python"
        plan = $WebPlan
        buildCommand = $commonBuildCommand
        startCommand = "gunicorn app.web_report_main:app --bind 0.0.0.0:`$PORT --workers 2 --timeout 120"
        healthCheckPath = "/healthz"
        exactKeys = @(
            "APP_TIMEZONE",
            "WEB_REPORT_REFRESH_SECONDS",
            "WEB_REPORT_HOST",
            "WEB_REPORT_PORT",
            "WEB_REPORT_STATUS_MAP_PATH",
            "REPORT_THB_TO_VND_RATE",
            "REPORT_THB_MINOR_UNIT_FACTOR"
        )
        prefixKeys = @(
            "TELEGRAM_",
            "META_",
            "PANCAKE_",
            "RECONCILE_COD_",
            "THAI_DUONG_",
            "REPORT_",
            "DAILY_REPORT_"
        )
        requiredAll = @(
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ALLOWED_USER_ID",
            "META_ACCESS_TOKEN",
            "META_AD_ACCOUNT_ID",
            "META_PAGE_ID",
            "PANCAKE_SHOP_ID"
        )
        requiredAny = @(
            @("PANCAKE_ACCESS_TOKEN", "PANCAKE_API_KEY")
        )
        defaults = @{
            "APP_TIMEZONE" = "Asia/Ho_Chi_Minh"
            "WEB_REPORT_REFRESH_SECONDS" = "600"
            "WEB_REPORT_HOST" = "0.0.0.0"
        }
    },
    @{
        name = "fb-ops-main-bot"
        type = "background_worker"
        runtime = "python"
        plan = $WorkerPlan
        buildCommand = $commonBuildCommand
        startCommand = "python -m app.main"
        exactKeys = @("APP_TIMEZONE")
        prefixKeys = @(
            "TELEGRAM_",
            "META_",
            "PANCAKE_",
            "DAILY_REPORT_",
            "RECONCILE_COD_",
            "THAI_DUONG_",
            "PANCAKE_TD_SYNC_",
            "TOKEN_HEALTHCHECK_",
            "REPORT_",
            "APP_"
        )
        requiredAll = @(
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ALLOWED_USER_ID",
            "META_ACCESS_TOKEN",
            "META_AD_ACCOUNT_ID",
            "META_PAGE_ID"
        )
        requiredAny = @()
        defaults = @{
            "APP_TIMEZONE" = "Asia/Ho_Chi_Minh"
        }
    },
    @{
        name = "fb-ops-media-bot"
        type = "background_worker"
        runtime = "python"
        plan = $WorkerPlan
        buildCommand = $commonBuildCommand
        startCommand = "python -m app.media_main"
        exactKeys = @("APP_TIMEZONE")
        prefixKeys = @(
            "MEDIA_BOT_",
            "MEDIA_RESEARCH_",
            "WORK_PROGRESS_"
        )
        requiredAll = @(
            "MEDIA_BOT_TELEGRAM_TOKEN",
            "MEDIA_BOT_ALLOWED_USER_ID"
        )
        requiredAny = @()
        defaults = @{
            "MEDIA_BOT_TIMEZONE" = "Asia/Ho_Chi_Minh"
        }
    },
    @{
        name = "fb-ops-assistant-bot"
        type = "background_worker"
        runtime = "python"
        plan = $WorkerPlan
        buildCommand = $commonBuildCommand
        startCommand = "python -m app.assistant_main"
        exactKeys = @()
        prefixKeys = @("BOT3_")
        requiredAll = @(
            "BOT3_TELEGRAM_TOKEN",
            "BOT3_ALLOWED_USER_ID",
            "BOT3_GOOGLE_OAUTH_CLIENT_ID",
            "BOT3_GOOGLE_OAUTH_CLIENT_SECRET",
            "BOT3_GOOGLE_OAUTH_REFRESH_TOKEN"
        )
        requiredAny = @()
        defaults = @{
            "BOT3_TIMEZONE" = "Asia/Ho_Chi_Minh"
        }
    }
)

foreach ($profile in $profiles) {
    Validate-RequiredEnv `
        -EnvMap $envMap `
        -ProfileName ([string]$profile.name) `
        -RequiredAll ([string[]]$profile.requiredAll) `
        -RequiredAnyGroups ($profile.requiredAny)

    $envVars = Build-EnvVarList `
        -EnvMap $envMap `
        -ExactKeys ([string[]]$profile.exactKeys) `
        -Prefixes ([string[]]$profile.prefixKeys) `
        -DefaultValues ([hashtable]$profile.defaults)

    if ($envVars.Count -eq 0) {
        throw "No env vars selected for profile '$([string]$profile.name)'."
    }

    Upsert-RenderService `
        -Profile $profile `
        -OwnerId $OwnerId `
        -RepoUrl $RepoUrl `
        -Branch $Branch `
        -RootDir $RootDir `
        -Region $Region `
        -Plan ([string]$profile.plan) `
        -EnvVars $envVars `
        -SkipDeployTrigger:$SkipDeployTrigger
}

Write-Host ""
Write-Host "Render stack sync completed."
