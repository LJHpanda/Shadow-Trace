[CmdletBinding()]
param(
    [string]$OutputRoot = "",
    [string]$Version = "",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $ProjectRoot "release-source"
}
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)

if (-not $Version) {
    $launcher = Get-Content -LiteralPath (Join-Path $ProjectRoot "desktop_launcher.py") -Raw
    if ($launcher -notmatch 'APP_VERSION\s*=\s*"v?([^"]+)"') {
        throw "Cannot read APP_VERSION from desktop_launcher.py"
    }
    $Version = $Matches[1]
}

if (-not $PythonExe) {
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $PythonExe = $venvPython
    } else {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $pythonCommand) { throw "Python not found; pass -PythonExe explicitly" }
        $PythonExe = $pythonCommand.Source
    }
}

& $PythonExe (Join-Path $ProjectRoot "scripts\public_release_check.py")
if ($LASTEXITCODE -ne 0) {
    throw "Public source verification failed"
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$ZipPath = Join-Path $OutputRoot "YingJi-$Version-source.zip"
if (Test-Path -LiteralPath $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}

& git -C $ProjectRoot archive --format=zip --output=$ZipPath HEAD
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) {
    throw "git archive failed"
}

$Hash = Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256
$ChecksumPath = Join-Path $OutputRoot "YingJi-$Version-source.sha256"
"$($Hash.Hash.ToLowerInvariant())  $([IO.Path]::GetFileName($ZipPath))" |
    Set-Content -LiteralPath $ChecksumPath -Encoding ascii

Write-Host "Public source archive: $ZipPath"
Write-Host "SHA-256: $($Hash.Hash.ToLowerInvariant())"
