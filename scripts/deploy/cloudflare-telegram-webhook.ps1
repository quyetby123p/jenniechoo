param(
    [string]$EnvFile = ".env",
    [string]$Repo = "quyetby123p/jenniechoo",
    [string]$WorkflowFile = "free-scheduled-tasks.yml",
    [string]$GitRef = "main",
    [string]$WorkerUrl = "",
    [string]$WebhookSecret = "",
    [switch]$SkipDeploy,
    [switch]$SkipSetWebhook
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

function Get-GitHubToken {
    $inputText = "protocol=https`nhost=github.com`n`n"
    $credential = $inputText | git credential fill 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $credential) {
        throw "Cannot read GitHub token from Git Credential Manager. Login/push to GitHub first, then rerun."
    }
    $passwordLine = $credential | Where-Object { $_ -like "password=*" } | Select-Object -First 1
    $token = ([string]$passwordLine) -replace "^password=", ""
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw "GitHub credential did not contain a token/password."
    }
    return $token.Trim()
}

function New-WebhookSecret {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return ([Convert]::ToBase64String($bytes)).TrimEnd("=").Replace("+", "_").Replace("/", "-")
}

function Set-EnvFileValue {
    param(
        [string]$Path,
        [string]$Name,
        [string]$Value
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $found = $false
    if (Test-Path $Path) {
        foreach ($line in Get-Content -Path $Path) {
            if ($line -match "^\s*$([regex]::Escape($Name))\s*=") {
                $lines.Add(("{0}={1}" -f $Name, $Value))
                $found = $true
            } else {
                $lines.Add($line)
            }
        }
    }
    if (-not $found) {
        if ($lines.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace($lines[$lines.Count - 1])) {
            $lines.Add("")
        }
        $lines.Add(("{0}={1}" -f $Name, $Value))
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines((Resolve-Path -Path (Split-Path -Parent $Path)).Path + "\" + (Split-Path -Leaf $Path), $lines, $utf8NoBom)
}

function Set-WranglerSecret {
    param(
        [string]$Name,
        [string]$Value,
        [string]$ConfigPath
    )
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return
    }
    $Value | npx wrangler secret put $Name --config $ConfigPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to set Cloudflare Worker secret: $Name"
    }
}

function Strip-Ansi {
    param([string]$Value)
    return $Value -replace "`e\[[0-9;?]*[ -/]*[@-~]", ""
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$workerRoot = Join-Path $projectRoot "workers\telegram-github-dispatcher"
$configPath = Join-Path $workerRoot "wrangler.toml"
$envPath = Join-Path $projectRoot $EnvFile

if (-not (Get-Command npx -ErrorAction SilentlyContinue)) {
    throw "Node.js/npx is required for Wrangler. Install Node.js LTS, then rerun this script."
}

$envValues = Read-EnvFile -Path $envPath
if ($envValues.Count -eq 0) {
    throw "No env values found in $EnvFile."
}

$telegramToken = [string]$envValues["TELEGRAM_BOT_TOKEN"]
$allowedUserId = [string]$envValues["TELEGRAM_ALLOWED_USER_ID"]
if ([string]::IsNullOrWhiteSpace($telegramToken) -or [string]::IsNullOrWhiteSpace($allowedUserId)) {
    throw "TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID are required in $EnvFile."
}
if ([string]::IsNullOrWhiteSpace($WebhookSecret)) {
    $WebhookSecret = [string]$envValues["TELEGRAM_WEBHOOK_SECRET"]
}
if ([string]::IsNullOrWhiteSpace($WebhookSecret)) {
    $WebhookSecret = New-WebhookSecret
    Set-EnvFileValue -Path $envPath -Name "TELEGRAM_WEBHOOK_SECRET" -Value $WebhookSecret
    Write-Host "Generated TELEGRAM_WEBHOOK_SECRET and saved it to $EnvFile."
}

$botInfo = Invoke-RestMethod -Method Get -Uri ("https://api.telegram.org/bot{0}/getMe" -f $telegramToken)
if (-not $botInfo.ok) {
    throw "Telegram getMe failed."
}
$botUsername = [string]$botInfo.result.username

$groupIds = New-Object System.Collections.Generic.List[string]
foreach ($key in @("DAILY_REPORT_NOTIFY_CHAT_ID", "RECONCILE_COD_NOTIFY_CHAT_ID", "PANCAKE_TD_SYNC_NOTIFY_CHAT_ID")) {
    $value = [string]$envValues[$key]
    if (-not [string]::IsNullOrWhiteSpace($value) -and $value.Trim() -ne "0") {
        $groupIds.Add($value.Trim())
    }
}
$allowedGroupChatIds = (($groupIds | Select-Object -Unique) -join ",")

$githubToken = Get-GitHubToken

Push-Location -Path $workerRoot
try {
    Write-Host "Setting Cloudflare Worker secrets..."
    Set-WranglerSecret -Name "TELEGRAM_BOT_TOKEN" -Value $telegramToken -ConfigPath $configPath
    Set-WranglerSecret -Name "TELEGRAM_WEBHOOK_SECRET" -Value $WebhookSecret -ConfigPath $configPath
    Set-WranglerSecret -Name "TELEGRAM_ALLOWED_USER_ID" -Value $allowedUserId -ConfigPath $configPath
    Set-WranglerSecret -Name "BOT_USERNAME" -Value $botUsername -ConfigPath $configPath
    Set-WranglerSecret -Name "ALLOWED_GROUP_CHAT_IDS" -Value $allowedGroupChatIds -ConfigPath $configPath
    Set-WranglerSecret -Name "GITHUB_TOKEN" -Value $githubToken -ConfigPath $configPath
    Set-WranglerSecret -Name "GITHUB_REPO" -Value $Repo -ConfigPath $configPath
    Set-WranglerSecret -Name "GITHUB_WORKFLOW_FILE" -Value $WorkflowFile -ConfigPath $configPath
    Set-WranglerSecret -Name "GITHUB_REF" -Value $GitRef -ConfigPath $configPath
    Set-WranglerSecret -Name "CLOUD_DISPATCH_ACK_ENABLED" -Value "1" -ConfigPath $configPath
    Set-WranglerSecret -Name "SCHEDULE_GUARD_SECRET" -Value $WebhookSecret -ConfigPath $configPath

    if (-not $SkipDeploy) {
        Write-Host "Deploying Cloudflare Worker..."
        $deployOutput = (& npx wrangler deploy --config $configPath 2>&1) | ForEach-Object { [string]$_ }
        $deployOutput | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) {
            throw "Wrangler deploy failed."
        }
        if ([string]::IsNullOrWhiteSpace($WorkerUrl)) {
            $plainOutput = Strip-Ansi -Value ($deployOutput -join "`n")
            $match = [regex]::Match($plainOutput, "https://[^\s]+\.workers\.dev")
            if ($match.Success) {
                $WorkerUrl = $match.Value.TrimEnd("/")
            }
        }
    }
} finally {
    Pop-Location
}

if ([string]::IsNullOrWhiteSpace($WorkerUrl)) {
    Write-Host "Worker deployed/secrets set, but worker URL was not detected."
    Write-Host "Rerun with -WorkerUrl https://<worker>.<subdomain>.workers.dev to set Telegram webhook."
    exit 0
}

$scheduleMarkUrl = $WorkerUrl.TrimEnd("/") + "/schedule/mark"
Set-EnvFileValue -Path $envPath -Name "CLOUD_SCHEDULE_GUARD_MARK_URL" -Value $scheduleMarkUrl
Set-EnvFileValue -Path $envPath -Name "CLOUD_SCHEDULE_GUARD_SECRET" -Value $WebhookSecret
Set-EnvFileValue -Path $envPath -Name "CLOUD_SCHEDULE_GUARD_ENABLED" -Value "1"
Write-Host ("Saved local schedule guard endpoint: {0}" -f $scheduleMarkUrl)

if (-not $SkipSetWebhook) {
    $webhookUrl = $WorkerUrl.TrimEnd("/") + "/telegram/webhook"
    Write-Host ("Setting Telegram webhook: {0}" -f $webhookUrl)
    $body = @{
        url = $webhookUrl
        secret_token = $WebhookSecret
        allowed_updates = '["message","callback_query"]'
        drop_pending_updates = "false"
    }
    $result = Invoke-RestMethod -Method Post -Uri ("https://api.telegram.org/bot{0}/setWebhook" -f $telegramToken) -Body $body
    if (-not $result.ok) {
        throw "Telegram setWebhook failed: $($result | ConvertTo-Json -Compress)"
    }
    Write-Host "Telegram webhook configured."
}

Write-Host "Cloudflare Telegram webhook setup completed."
