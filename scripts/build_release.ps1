[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$PythonExe = "",
    [string]$RuntimeSource = "",
    [string]$OutputRoot = "",
    [string]$SigningThumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [switch]$AllowUnsigned,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if (-not $Version) {
    $launcher = Get-Content -LiteralPath (Join-Path $ProjectRoot "desktop_launcher.py") -Raw
    if ($launcher -notmatch 'APP_VERSION\s*=\s*"([^"]+)"') {
        throw "Cannot read APP_VERSION from desktop_launcher.py"
    }
    $Version = $Matches[1]
}

if (-not $PythonExe) {
    $PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Build Python not found: $PythonExe"
}

if (-not $RuntimeSource) {
    $RuntimeSource = Join-Path $ProjectRoot "Runtime"
}
$RuntimeSource = (Resolve-Path -LiteralPath $RuntimeSource).Path
foreach ($binary in "ffmpeg.exe", "ffprobe.exe") {
    if (-not (Test-Path -LiteralPath (Join-Path $RuntimeSource $binary) -PathType Leaf)) {
        throw "Runtime binary not found: $RuntimeSource\$binary"
    }
}

if (-not $OutputRoot) {
    $OutputRoot = Join-Path $ProjectRoot "release"
}
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
$BuildRoot = Join-Path $ProjectRoot "build-work\community-release"
$DistRoot = Join-Path $BuildRoot "dist"
$WorkRoot = Join-Path $BuildRoot "pyinstaller"
$ReleaseRoot = Join-Path $OutputRoot $Version
$InstallerOutput = Join-Path $ReleaseRoot "installer"
$PortableOutput = Join-Path $ReleaseRoot "portable"

foreach ($target in $BuildRoot, $ReleaseRoot) {
    $resolvedParent = [IO.Path]::GetFullPath((Split-Path -Parent $target))
    $resolvedProject = [IO.Path]::GetFullPath($ProjectRoot)
    if (-not $resolvedParent.StartsWith($resolvedProject, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean outside project directory: $target"
    }
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $DistRoot, $WorkRoot, $InstallerOutput, $PortableOutput -Force | Out-Null

& $PythonExe (Join-Path $ProjectRoot "scripts\public_release_check.py")
if ($LASTEXITCODE -ne 0) {
    throw "Public source verification failed"
}

& $PythonExe -m PyInstaller --noconfirm --clean `
    --distpath $DistRoot `
    --workpath $WorkRoot `
    (Join-Path $ProjectRoot "YingJi.spec")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed"
}

$AppDir = Join-Path $DistRoot "YingJi"
$BundledRuntime = Join-Path $AppDir "Runtime"
Copy-Item -LiteralPath (Join-Path $RuntimeSource "ffmpeg.exe") -Destination $BundledRuntime -Force
Copy-Item -LiteralPath (Join-Path $RuntimeSource "ffprobe.exe") -Destination $BundledRuntime -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "legal") -Destination (Join-Path $AppDir "legal") -Recurse -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "LICENSE") -Destination (Join-Path $AppDir "LICENSE") -Force
foreach ($document in "README.md", "SECURITY.md", "CHANGELOG.md") {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot $document) -Destination (Join-Path $AppDir $document) -Force
}

function Find-SignTool {
    $command = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $programFilesX86 = [Environment]::GetFolderPath("ProgramFilesX86")
    $kitsRoot = Join-Path $programFilesX86 "Windows Kits\10\bin"
    if (Test-Path -LiteralPath $kitsRoot) {
        $kits = Get-ChildItem $kitsRoot -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($kits) { return $kits.FullName }
    }
    return $null
}

function Sign-Artifact([string]$Path) {
    if (-not $SigningThumbprint) { return }
    $signTool = Find-SignTool
    if (-not $signTool) { throw "Signing thumbprint provided, but signtool.exe was not found" }
    & $signTool sign /sha1 $SigningThumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 $Path
    if ($LASTEXITCODE -ne 0) { throw "Signing failed: $Path" }
}

$MainExe = Join-Path $AppDir "YingJi.exe"
Sign-Artifact $MainExe
$signature = Get-AuthenticodeSignature -LiteralPath $MainExe
if ($signature.Status -ne "Valid" -and -not $AllowUnsigned) {
    throw "YingJi.exe is not Authenticode-valid ($($signature.Status)). Provide -SigningThumbprint. Use -AllowUnsigned only for internal previews."
}

$VerificationFile = Join-Path $ReleaseRoot "portable-verification.json"
$verificationArgs = @($AppDir, "--json-output", $VerificationFile)
& $PythonExe (Join-Path $ProjectRoot "scripts\verify_release.py") @verificationArgs
if ($LASTEXITCODE -ne 0) { throw "Release cleanliness verification failed" }

$PortableDir = Join-Path $PortableOutput "YingJi"
Copy-Item -LiteralPath $AppDir -Destination $PortableDir -Recurse -Force
Set-Content -LiteralPath (Join-Path $PortableDir "portable.flag") -Value "portable" -Encoding ascii
$PortableZip = Join-Path $ReleaseRoot "YingJi-$Version-portable-x64.zip"
Compress-Archive -LiteralPath $PortableDir -DestinationPath $PortableZip -CompressionLevel Optimal

$InstallerExe = $null
if (-not $SkipInstaller) {
    $makeNsis = Get-Command "makensis.exe" -ErrorAction SilentlyContinue
    if (-not $makeNsis) {
        $programFilesX86 = [Environment]::GetFolderPath("ProgramFilesX86")
        $candidate = Join-Path $programFilesX86 "NSIS\makensis.exe"
        if (Test-Path -LiteralPath $candidate) { $makeNsis = Get-Item -LiteralPath $candidate }
    }
    if (-not $makeNsis) {
        throw "NSIS not found. Install NSIS 3 and retry, or use -SkipInstaller for an internal portable preview."
    }
    $makeNsisPath = if ($makeNsis -is [Management.Automation.CommandInfo]) {
        $makeNsis.Source
    } else {
        $makeNsis.FullName
    }
    $InstallerPath = Join-Path $InstallerOutput "YingJi-Setup-$Version-x64.exe"
    & $makeNsisPath `
        "/DMyAppVersion=$Version" `
        "/DSourceDir=$AppDir" `
        "/DOutputFile=$InstallerPath" `
        (Join-Path $ProjectRoot "installer\YingJi.nsi")
    if ($LASTEXITCODE -ne 0) { throw "Installer build failed" }
    $InstallerExe = Get-Item -LiteralPath $InstallerPath -ErrorAction SilentlyContinue
    if (-not $InstallerExe) { throw "NSIS compiler produced no EXE" }
    Sign-Artifact $InstallerExe.FullName
    $installerSignature = Get-AuthenticodeSignature -LiteralPath $InstallerExe.FullName
    if ($installerSignature.Status -ne "Valid" -and -not $AllowUnsigned) {
        throw "Installer is not Authenticode-valid ($($installerSignature.Status))"
    }
}

$artifacts = @($PortableZip)
if ($InstallerExe) { $artifacts += $InstallerExe.FullName }
$hashLines = foreach ($artifact in $artifacts) {
    $hash = Get-FileHash -LiteralPath $artifact -Algorithm SHA256
    "$($hash.Hash.ToLowerInvariant())  $([IO.Path]::GetFileName($artifact))"
}
$hashLines | Set-Content -LiteralPath (Join-Path $ReleaseRoot "SHA256SUMS.txt") -Encoding utf8

$sourceCommit = (& git -C $ProjectRoot rev-parse HEAD | Out-String).Trim()
$pythonVersion = (& $PythonExe --version 2>&1 | Out-String).Trim()
$requirementsHash = (Get-FileHash -LiteralPath (Join-Path $ProjectRoot "requirements-build.txt") -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest = [ordered]@{
    product = "YingJi"
    version = $Version
    built_at = (Get-Date).ToUniversalTime().ToString("o")
    source_commit = $sourceCommit
    python = $pythonVersion
    requirements_build_sha256 = $requirementsHash
    runtime_source = $RuntimeSource
    main_exe_signature = $signature.Status.ToString()
    installer_built = [bool]$InstallerExe
    unsigned_preview = [bool]($signature.Status -ne "Valid")
    artifacts = @($artifacts | ForEach-Object { [IO.Path]::GetFileName($_) })
}
$manifest |
    ConvertTo-Json -Depth 5 |
    Set-Content -LiteralPath (Join-Path $ReleaseRoot "build-manifest.json") -Encoding utf8

Write-Host "Release build completed: $ReleaseRoot"
Write-Host "Main executable signature: $($signature.Status)"
if ($AllowUnsigned) {
    Write-Warning "This community build is unsigned. Verify its SHA-256 checksum before use."
}
