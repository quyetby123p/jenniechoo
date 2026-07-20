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
$bot3TelegramToken = [string]$envValues["BOT3_TELEGRAM_TOKEN"]
$bot3AllowedUserId = [string]$envValues["BOT3_ALLOWED_USER_ID"]
if ([string]::IsNullOrWhiteSpace($bot3AllowedUserId)) {
    $bot3AllowedUserId = $allowedUserId
}
$ads2TelegramToken = [string]$envValues["ADS2_TELEGRAM_BOT_TOKEN"]
$ads2AllowedUserId = [string]$envValues["ADS2_TELEGRAM_ALLOWED_USER_ID"]
if ([string]::IsNullOrWhiteSpace($ads2AllowedUserId)) {
    $ads2AllowedUserId = $allowedUserId
}
$mediaTelegramToken = [string]$envValues["MEDIA_BOT_TELEGRAM_TOKEN"]
$mediaAllowedUserId = [string]$envValues["MEDIA_BOT_ALLOWED_USER_ID"]
if ([string]::IsNullOrWhiteSpace($mediaAllowedUserId)) {
    $mediaAllowedUserId = $allowedUserId
}
if ([string]::IsNullOrWhiteSpace($WebhookSecret)) {
    $WebhookSecret = [string]$envValues["TELEGRAM_WEBHOOK_SECRET"]
}
if ([string]::IsNullOrWhiteSpace($WebhookSecret)) {
    $WebhookSecret = New-WebhookSecret
    Set-EnvFileValue -Path $envPath -Name "TELEGRAM_WEBHOOK_SECRET" -Value $WebhookSecret
    Write-Host "Generated TELEGRAM_WEBHOOK_SECRET and saved it to $EnvFile."
}
$ads2WebhookSecret = [string]$envValues["ADS2_TELEGRAM_WEBHOOK_SECRET"]
if ([string]::IsNullOrWhiteSpace($ads2WebhookSecret)) {
    $ads2WebhookSecret = $WebhookSecret
}

$botInfo = Invoke-RestMethod -Method Get -Uri ("https://api.telegram.org/bot{0}/getMe" -f $telegramToken)
if (-not $botInfo.ok) {
    throw "Telegram getMe failed."
}
$botUsername = [string]$botInfo.result.username
$bot3Username = ""
if (-not [string]::IsNullOrWhiteSpace($bot3TelegramToken)) {
    $bot3Info = Invoke-RestMethod -Method Get -Uri ("https://api.telegram.org/bot{0}/getMe" -f $bot3TelegramToken)
    if (-not $bot3Info.ok) {
        throw "Bot 3 Telegram getMe failed."
    }
    $bot3Username = [string]$bot3Info.result.username
}
$ads2Username = ""
if (-not [string]::IsNullOrWhiteSpace($ads2TelegramToken)) {
    $ads2Info = Invoke-RestMethod -Method Get -Uri ("https://api.telegram.org/bot{0}/getMe" -f $ads2TelegramToken)
    if (-not $ads2Info.ok) {
        throw "ADS2 Telegram getMe failed."
    }
    $ads2Username = [string]$ads2Info.result.username
}
$mediaUsername = ""
if (-not [string]::IsNullOrWhiteSpace($mediaTelegramToken)) {
    $mediaInfo = Invoke-RestMethod -Method Get -Uri ("https://api.telegram.org/bot{0}/getMe" -f $mediaTelegramToken)
    if (-not $mediaInfo.ok) {
        throw "Media Bot Telegram getMe failed."
    }
    $mediaUsername = [string]$mediaInfo.result.username
}

$groupIds = New-Object System.Collections.Generic.List[string]
foreach ($key in @("DAILY_REPORT_NOTIFY_CHAT_ID", "RECONCILE_COD_NOTIFY_CHAT_ID", "PANCAKE_TD_SYNC_NOTIFY_CHAT_ID")) {
    $value = [string]$envValues[$key]
    if (-not [string]::IsNullOrWhiteSpace($value) -and $value.Trim() -ne "0") {
        $groupIds.Add($value.Trim())
    }
}
$allowedGroupChatIds = (($groupIds | Select-Object -Unique) -join ",")
$bot3GroupIds = New-Object System.Collections.Generic.List[string]
foreach ($key in @("BOT3_TASK_GROUP_CHAT_ID")) {
    $value = [string]$envValues[$key]
    if (-not [string]::IsNullOrWhiteSpace($value) -and $value.Trim() -ne "0") {
        $bot3GroupIds.Add($value.Trim())
    }
}
$bot3AllowedGroupChatIds = (($bot3GroupIds | Select-Object -Unique) -join ",")
$mediaGroupIds = New-Object System.Collections.Generic.List[string]
foreach ($key in @("MEDIA_BOT_ALLOWED_GROUP_CHAT_IDS", "WORK_PROGRESS_TELEGRAM_ALLOWLIST_CHANNEL_IDS")) {
    $value = [string]$envValues[$key]
    if (-not [string]::IsNullOrWhiteSpace($value)) {
        foreach ($part in $value.Split(",")) {
            $trimmed = $part.Trim()
            if (-not [string]::IsNullOrWhiteSpace($trimmed) -and $trimmed -ne "0" -and $trimmed -ne "__DISABLED__") {
                $mediaGroupIds.Add($trimmed)
            }
        }
    }
}
$mediaAllowedGroupChatIds = (($mediaGroupIds | Select-Object -Unique) -join ",")

$githubToken = Get-GitHubToken

Push-Location -Path $workerRoot
try {
    Write-Host "Setting Cloudflare Worker secrets..."
    Set-WranglerSecret -Name "TELEGRAM_BOT_TOKEN" -Value $telegramToken -ConfigPath $configPath
    Set-WranglerSecret -Name "TELEGRAM_WEBHOOK_SECRET" -Value $WebhookSecret -ConfigPath $configPath
    Set-WranglerSecret -Name "TELEGRAM_ALLOWED_USER_ID" -Value $allowedUserId -ConfigPath $configPath
    Set-WranglerSecret -Name "BOT_USERNAME" -Value $botUsername -ConfigPath $configPath
    Set-WranglerSecret -Name "ALLOWED_GROUP_CHAT_IDS" -Value $allowedGroupChatIds -ConfigPath $configPath
    Set-WranglerSecret -Name "BOT3_TELEGRAM_TOKEN" -Value $bot3TelegramToken -ConfigPath $configPath
    Set-WranglerSecret -Name "BOT3_ALLOWED_USER_ID" -Value $bot3AllowedUserId -ConfigPath $configPath
    Set-WranglerSecret -Name "BOT3_USERNAME" -Value $bot3Username -ConfigPath $configPath
    Set-WranglerSecret -Name "BOT3_TASK_GROUP_CHAT_ID" -Value ([string]$envValues["BOT3_TASK_GROUP_CHAT_ID"]) -ConfigPath $configPath
    Set-WranglerSecret -Name "BOT3_ALLOWED_GROUP_CHAT_IDS" -Value $bot3AllowedGroupChatIds -ConfigPath $configPath
    Set-WranglerSecret -Name "BOT3_TELEGRAM_WEBHOOK_SECRET" -Value $WebhookSecret -ConfigPath $configPath
    Set-WranglerSecret -Name "ADS2_TELEGRAM_BOT_TOKEN" -Value $ads2TelegramToken -ConfigPath $configPath
    Set-WranglerSecret -Name "ADS2_TELEGRAM_ALLOWED_USER_ID" -Value $ads2AllowedUserId -ConfigPath $configPath
    Set-WranglerSecret -Name "ADS2_BOT_USERNAME" -Value $ads2Username -ConfigPath $configPath
    Set-WranglerSecret -Name "ADS2_ALLOWED_GROUP_CHAT_IDS" -Value $allowedGroupChatIds -ConfigPath $configPath
    Set-WranglerSecret -Name "ADS2_TELEGRAM_WEBHOOK_SECRET" -Value $ads2WebhookSecret -ConfigPath $configPath
    Set-WranglerSecret -Name "MEDIA_BOT_TELEGRAM_TOKEN" -Value $mediaTelegramToken -ConfigPath $configPath
    Set-WranglerSecret -Name "MEDIA_BOT_ALLOWED_USER_ID" -Value $mediaAllowedUserId -ConfigPath $configPath
    Set-WranglerSecret -Name "MEDIA_BOT_USERNAME" -Value $mediaUsername -ConfigPath $configPath
    Set-WranglerSecret -Name "MEDIA_BOT_ALLOWED_GROUP_CHAT_IDS" -Value $mediaAllowedGroupChatIds -ConfigPath $configPath
    Set-WranglerSecret -Name "WORK_PROGRESS_TELEGRAM_ALLOWLIST_CHANNEL_IDS" -Value ([string]$envValues["WORK_PROGRESS_TELEGRAM_ALLOWLIST_CHANNEL_IDS"]) -ConfigPath $configPath
    Set-WranglerSecret -Name "MEDIA_BOT_TELEGRAM_WEBHOOK_SECRET" -Value $WebhookSecret -ConfigPath $configPath
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

    if (-not [string]::IsNullOrWhiteSpace($bot3TelegramToken)) {
        $bot3WebhookUrl = $WorkerUrl.TrimEnd("/") + "/telegram/webhook/bot3"
        Write-Host ("Setting Bot 3 Telegram webhook: {0}" -f $bot3WebhookUrl)
        $bot3Body = @{
            url = $bot3WebhookUrl
            secret_token = $WebhookSecret
            allowed_updates = '["message","callback_query"]'
            drop_pending_updates = "false"
        }
        $bot3Result = Invoke-RestMethod -Method Post -Uri ("https://api.telegram.org/bot{0}/setWebhook" -f $bot3TelegramToken) -Body $bot3Body
        if (-not $bot3Result.ok) {
            throw "Bot 3 Telegram setWebhook failed: $($bot3Result | ConvertTo-Json -Compress)"
        }
        Write-Host "Bot 3 Telegram webhook configured."
    }
    if (-not [string]::IsNullOrWhiteSpace($ads2TelegramToken)) {
        $ads2WebhookUrl = $WorkerUrl.TrimEnd("/") + "/telegram/webhook/ads2"
        Write-Host ("Setting ADS2 Telegram webhook: {0}" -f $ads2WebhookUrl)
        $ads2Body = @{
            url = $ads2WebhookUrl
            secret_token = $ads2WebhookSecret
            allowed_updates = '["message","callback_query"]'
            drop_pending_updates = "false"
        }
        $ads2Result = Invoke-RestMethod -Method Post -Uri ("https://api.telegram.org/bot{0}/setWebhook" -f $ads2TelegramToken) -Body $ads2Body
        if (-not $ads2Result.ok) {
            throw "ADS2 Telegram setWebhook failed: $($ads2Result | ConvertTo-Json -Compress)"
        }
        Write-Host "ADS2 Telegram webhook configured."
    }
    if (-not [string]::IsNullOrWhiteSpace($mediaTelegramToken)) {
        $mediaWebhookUrl = $WorkerUrl.TrimEnd("/") + "/telegram/webhook/media"
        Write-Host ("Setting Media Bot Telegram webhook: {0}" -f $mediaWebhookUrl)
        $mediaBody = @{
            url = $mediaWebhookUrl
            secret_token = $WebhookSecret
            allowed_updates = '["message","callback_query"]'
            drop_pending_updates = "false"
        }
        $mediaResult = Invoke-RestMethod -Method Post -Uri ("https://api.telegram.org/bot{0}/setWebhook" -f $mediaTelegramToken) -Body $mediaBody
        if (-not $mediaResult.ok) {
            throw "Media Bot Telegram setWebhook failed: $($mediaResult | ConvertTo-Json -Compress)"
        }
        Write-Host "Media Bot Telegram webhook configured."
    }
}

Write-Host "Cloudflare Telegram webhook setup completed."
