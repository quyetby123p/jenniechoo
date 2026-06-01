param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [Parameter(Mandatory = $true)]
    [string]$PythonExe
)

$ErrorActionPreference = "Stop"
Set-Location -Path $ProjectRoot

& $PythonExe "-m" "app.assistant_main"
