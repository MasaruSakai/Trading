param(
    [string]$HostName = "0.0.0.0",
    [int]$Port = 18180,
    [string]$Allow = "",
    [string]$Target = "",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "kabu_proxy_stop.ps1") -Port $Port

$startArgs = @{
    HostName = $HostName
    Port = $Port
    Python = $Python
}

if ($Allow) {
    $startArgs.Allow = $Allow
}

if ($Target) {
    $startArgs.Target = $Target
}

& (Join-Path $PSScriptRoot "kabu_proxy_start.ps1") @startArgs
