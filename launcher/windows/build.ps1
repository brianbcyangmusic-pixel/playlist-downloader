# Builds the Windows release: tray app + frozen backend, zipped for GitHub Releases.
#
#   .\build.ps1                 full build (creates .venv-win on first run)
#   .\build.ps1 -SkipBackend    reuse build\backend from an earlier run
#
# Requires: Python 3.11+ (py launcher) and the .NET SDK 10.

param(
    [string]$Version = "",
    [switch]$SkipBackend
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

if (-not $Version) {
    $Version = (Select-String -Path "app\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
}
Write-Host "==> Playlist Downloader $Version (Windows x64)"

$Build = Join-Path $Root "build\windows"
$Publish = Join-Path $Build "app"
$Dist = Join-Path $Root "dist\PlaylistDownloader-Windows-x64"
$Zip = Join-Path $Root "dist\PlaylistDownloader-Windows-x64.zip"

Remove-Item -Recurse -Force $Build, $Dist -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Build, $Dist | Out-Null

if (-not $SkipBackend) {
    $Venv = Join-Path $Root ".venv-win"
    $Py = Join-Path $Venv "Scripts\python.exe"
    if (-not (Test-Path $Py)) {
        Write-Host "==> Creating .venv-win"
        py -3 -m venv $Venv
    }
    Write-Host "==> Installing build dependencies"
    & $Py -m pip install --quiet --upgrade pip
    & $Py -m pip install --quiet -r requirements.txt pyinstaller
    Write-Host "==> Freezing backend"
    & $Py scripts\build_backend.py --out (Join-Path $Build "backend")
    if ($LASTEXITCODE -ne 0) { throw "backend build failed" }
}

Write-Host "==> Publishing tray app"
dotnet publish "launcher\windows\PlaylistDownloaderTray.csproj" `
    -c Release -r win-x64 --self-contained true `
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true `
    -p:Version=$Version -o $Publish
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed" }

Copy-Item (Join-Path $Publish "PlaylistDownloader.exe") $Dist
Copy-Item -Recurse (Join-Path $Build "backend\PlaylistDownloaderBackend") (Join-Path $Dist "backend")
Copy-Item (Join-Path $PSScriptRoot "install.ps1") $Dist
Copy-Item (Join-Path $PSScriptRoot "uninstall.ps1") $Dist
Copy-Item (Join-Path $PSScriptRoot "README.txt") $Dist

Compress-Archive -Path "$Dist\*" -DestinationPath $Zip -Force
Write-Host ""
Write-Host "Done: $Zip"
