param(
    [string]$HostName = "0.0.0.0",
    [int]$Port = 18180,
    [string]$Allow = "",
    [string]$Target = "",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$LogDir = Join-Path $ProjectRoot "logs"
$PidFile = Join-Path $LogDir "kabu_proxy.pid"
$Stdout = Join-Path $LogDir "kabu_proxy_stdout.log"
$Stderr = Join-Path $LogDir "kabu_proxy_stderr.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not $Allow) {
    if ($env:KABU_PROXY_ALLOW) {
        $Allow = $env:KABU_PROXY_ALLOW
    } else {
        $Allow = "127.0.0.1,::1,10.215.1.136"
    }
}

$existing = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine -match "kabu_proxy\.py" }

if ($existing) {
    $ids = ($existing | ForEach-Object { $_.ProcessId }) -join ", "
    Write-Host "kabu proxy already running: $ids"
    exit 0
}

$args = @(
    "kabu_station\kabu_proxy.py",
    "--host", $HostName,
    "--port", [string]$Port,
    "--allow", $Allow
)

if ($Target) {
    $args += @("--target", $Target)
}

function Quote-Arg($value) {
    $text = [string]$value
    if ($text -match '[\s"]') {
        return '"' + ($text -replace '"', '\"') + '"'
    }
    return $text
}

$quotedArgs = ($args | ForEach-Object { Quote-Arg $_ }) -join " "
$command = "cd /d " + (Quote-Arg $ProjectRoot) + " && " +
    (Quote-Arg $Python) + " " + $quotedArgs + " 1>>" +
    (Quote-Arg $Stdout) + " 2>>" + (Quote-Arg $Stderr)

# WScript.Shell detaches cleanly from SSH sessions. Start-Process can leave the
# remote SSH command waiting on inherited handles even after the proxy is up.
$shell = New-Object -ComObject WScript.Shell
$null = $shell.Run("cmd.exe /c $command", 0, $false)

Start-Sleep -Seconds 1

$proc = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine -match "kabu_proxy\.py" } |
    Select-Object -First 1

if ($proc) {
    Set-Content -Path $PidFile -Value $proc.ProcessId -Encoding ascii
}

$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    Write-Error "kabu proxy launch was requested, but port $Port is not listening"
}

if ($proc) {
    Write-Host "kabu proxy started: pid=$($proc.ProcessId) port=$Port allow=$Allow"
} else {
    Write-Host "kabu proxy started: port=$Port allow=$Allow"
}
