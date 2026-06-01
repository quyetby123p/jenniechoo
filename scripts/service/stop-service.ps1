param(
    [string]$ServiceName = "FBAdsTelegramBot"
)

$ErrorActionPreference = "Stop"
Stop-Service -Name $ServiceName -Force
Get-Service -Name $ServiceName | Select-Object Name, Status, StartType
