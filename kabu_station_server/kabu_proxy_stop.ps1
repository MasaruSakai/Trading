param(
    [int]$Port = 18180
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$PidFile = Join-Path (Join-Path $ProjectRoot "logs") "kabu_proxy.pid"

$targets = @()

if (Test-Path $PidFile) {
    $pidText = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($pidText -match "^\d+$") {
        $proc = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
        if ($proc) {
            $targets += $proc
        }
    }
}

$cmdlineMatches = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine -match "kabu_proxy\.py" }

foreach ($match in $cmdlineMatches) {
    $proc = Get-Process -Id $match.ProcessId -ErrorAction SilentlyContinue
    if ($proc -and -not ($targets | Where-Object { $_.Id -eq $proc.Id })) {
        $targets += $proc
    }
}

$portMatches = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($conn in $portMatches) {
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    if ($proc -and -not ($targets | Where-Object { $_.Id -eq $proc.Id })) {
        $targets += $proc
    }
}

if (-not $targets) {
    Write-Host "kabu proxy is not running"
    if (Test-Path $PidFile) {
        Remove-Item $PidFile -Force
    }
    exit 0
}

foreach ($proc in $targets) {
    Write-Host "stopping kabu proxy pid=$($proc.Id)"
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 1

if (Test-Path $PidFile) {
    Remove-Item $PidFile -Force
}

$stillListening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($stillListening) {
    Write-Error "port $Port is still listening after stop"
}

Write-Host "kabu proxy stopped"
