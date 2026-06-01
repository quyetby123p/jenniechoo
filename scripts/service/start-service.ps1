param(
    [string]$ServiceName = "FBAdsTelegramBot"
)

$ErrorActionPreference = "Stop"
Start-Service -Name $ServiceName
Get-Service -Name $ServiceName | Select-Object Name, Status, StartType
