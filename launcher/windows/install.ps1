# Installs Playlist Downloader for the current user (no admin prompt).
#
#   .\install.ps1                     install + launch
#   .\install.ps1 -StartWithWindows   also start it when you sign in
#
# Files go to %LOCALAPPDATA%\Programs\PlaylistDownloader and a Start Menu
# shortcut is created. Settings and downloads live elsewhere, so reinstalling
# never touches them.

param([switch]$StartWithWindows)

$ErrorActionPreference = "Stop"
$Source = $PSScriptRoot
$Dest = Join-Path $env:LOCALAPPDATA "Programs\PlaylistDownloader"
$Exe = Join-Path $Dest "PlaylistDownloader.exe"

Write-Host "==> Installing to $Dest"
Get-Process PlaylistDownloader -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Dest | Out-Null
Copy-Item -Recurse -Force (Join-Path $Source "*") $Dest

Write-Host "==> Creating Start Menu shortcut"
$shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Playlist Downloader.lnk"
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
$link.TargetPath = $Exe
$link.WorkingDirectory = $Dest
$link.IconLocation = $Exe
$link.Description = "Playlist Downloader"
$link.Save()

if ($StartWithWindows) {
    Write-Host "==> Registering autostart"
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
        -Name "PlaylistDownloader" -Value "`"$Exe`""
}

Write-Host "==> Starting Playlist Downloader"
Start-Process $Exe
Write-Host ""
Write-Host "Done. Look for the download icon in the notification area (click ^ to show hidden icons)."
