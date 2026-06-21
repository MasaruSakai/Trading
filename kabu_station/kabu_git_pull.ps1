param(
    [string]$Git = "git",
    [string]$Branch = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

if ($Git -eq "git") {
    $cmd = Get-Command git -ErrorAction SilentlyContinue
    if ($cmd) {
        $Git = $cmd.Source
    } else {
        $candidates = @(
            "C:\Program Files\Git\cmd\git.exe",
            "C:\Program Files\Git\bin\git.exe",
            "C:\Program Files (x86)\Git\cmd\git.exe",
            "C:\Program Files (x86)\Git\bin\git.exe"
        )
        foreach ($candidate in $candidates) {
            if (Test-Path $candidate) {
                $Git = $candidate
                break
            }
        }
    }
}

if (-not (Test-Path $Git) -and -not (Get-Command $Git -ErrorAction SilentlyContinue)) {
    throw "git executable not found. Pass -Git C:\path\to\git.exe or add git to PATH."
}

if ($Branch) {
    & $Git fetch --all --prune
    & $Git checkout $Branch
}

& $Git pull --ff-only
