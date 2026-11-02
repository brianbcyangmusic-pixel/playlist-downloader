# Removes Playlist Downloader from this account.
#
# Settings, the download queue and your music are left alone:
#   %APPDATA%\PlaylistDownloader          settings + queue
#   %USERPROFILE%\Downloads\Playlist Downloader
# Delete those folders yourself if you want a clean slate.

$ErrorActionPreference = "Stop"
$Dest = Join-Path $env:LOCALAPPDATA "Programs\PlaylistDownloader"

Write-Host "==> Stopping Playlist Downloader"
Get-Process PlaylistDownloader -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

Write-Host "==> Removing $Dest"
Remove-Item -Recurse -Force $Dest -ErrorAction SilentlyContinue

$shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Playlist Downloader.lnk"
Remove-Item -Force $shortcut -ErrorAction SilentlyContinue

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
Remove-ItemProperty -Path $runKey -Name "PlaylistDownloader" -ErrorAction SilentlyContinue

Write-Host "Done."
