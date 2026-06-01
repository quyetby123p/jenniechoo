param(
    [Parameter(Mandatory = $true)]
    [string]$GitHubRepoUrl,
    [string]$Branch = "main",
    [string]$CommitMessage = "feat: web report v1 and render deploy",
    [switch]$SkipTests,
    [switch]$NoPush,
    [string]$GitUserName = "",
    [string]$GitUserEmail = ""
)

$ErrorActionPreference = "Stop"

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Args
    )
    & git @Args
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: git $($Args -join ' ')"
    }
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -Path $projectRoot

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is not installed. Install Git first, then rerun script."
}

if (-not (Test-Path (Join-Path $projectRoot "render.yaml"))) {
    throw "Missing render.yaml at project root: $projectRoot"
}

$currentGitUserName = [string](& git config --get user.name)
$currentGitUserName = $currentGitUserName.Trim()
$currentGitUserEmail = [string](& git config --get user.email)
$currentGitUserEmail = $currentGitUserEmail.Trim()
if ([string]::IsNullOrWhiteSpace($currentGitUserName) -and -not [string]::IsNullOrWhiteSpace($GitUserName)) {
    Invoke-Git -Args @("config", "user.name", $GitUserName)
    $currentGitUserName = $GitUserName
}
if ([string]::IsNullOrWhiteSpace($currentGitUserEmail) -and -not [string]::IsNullOrWhiteSpace($GitUserEmail)) {
    Invoke-Git -Args @("config", "user.email", $GitUserEmail)
    $currentGitUserEmail = $GitUserEmail
}
if ([string]::IsNullOrWhiteSpace($currentGitUserName) -or [string]::IsNullOrWhiteSpace($currentGitUserEmail)) {
    throw "Git identity missing. Set git user.name/user.email or rerun with -GitUserName and -GitUserEmail."
}

if (-not (Test-Path (Join-Path $projectRoot ".git"))) {
    Invoke-Git -Args @("init")
}

Invoke-Git -Args @("checkout", "-B", $Branch)

if (-not $SkipTests) {
    $pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $pythonExe)) {
        if (Get-Command python -ErrorAction SilentlyContinue) {
            $pythonExe = "python"
        } else {
            throw "Python not found. Install Python or create .venv before running tests."
        }
    }

    & $pythonExe -m pytest tests/test_web_report_service.py tests/test_web_report_app.py -q
    if ($LASTEXITCODE -ne 0) {
        throw "Tests failed. Fix tests before pushing."
    }
}

Invoke-Git -Args @("add", ".")

& git diff --cached --quiet
$hasChangesToCommit = ($LASTEXITCODE -ne 0)

if ($hasChangesToCommit) {
    Invoke-Git -Args @("commit", "-m", $CommitMessage)
} else {
    Write-Host "No staged changes to commit."
}

& git remote get-url origin *> $null
if ($LASTEXITCODE -eq 0) {
    $originUrl = (& git remote get-url origin).Trim()
    if ($originUrl -ne $GitHubRepoUrl) {
        Invoke-Git -Args @("remote", "set-url", "origin", $GitHubRepoUrl)
    }
} else {
    Invoke-Git -Args @("remote", "add", "origin", $GitHubRepoUrl)
}

if ($NoPush) {
    Write-Host "Done. No push requested (-NoPush)."
    Write-Host "When ready, run: git push -u origin $Branch"
    exit 0
}

Invoke-Git -Args @("push", "-u", "origin", $Branch)
Write-Host "Done. Next step: connect this GitHub repo to Render and deploy from render.yaml"
