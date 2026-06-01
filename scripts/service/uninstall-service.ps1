param(
    [string]$ServiceName = "FBAdsTelegramBot"
)

$ErrorActionPreference = "Stop"

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
    if ($service.Status -ne "Stopped") {
        Stop-Service -Name $ServiceName -Force
    }
    sc.exe delete $ServiceName | Out-Null
    Write-Output "Da go service $ServiceName"
} else {
    Write-Output "Service $ServiceName khong ton tai."
}
