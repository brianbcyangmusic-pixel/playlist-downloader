Playlist Downloader — Windows tray app
======================================

Install
-------
1. Unzip PlaylistDownloader-Windows-x64.zip anywhere (it is portable).
2. Run install.ps1 to copy it to %LOCALAPPDATA%\Programs\PlaylistDownloader,
   create a Start Menu shortcut and start it.
   Use:  powershell -ExecutionPolicy Bypass -File install.ps1
   Add -StartWithWindows to launch it when you sign in.

You can also just run PlaylistDownloader.exe in place — the tray app works
from any folder as long as the backend\ folder stays next to the exe.

The tray icon
-------------
* Left click / double click the icon in the notification area (may be hidden
  behind the ^ arrow) to see the menu.
* The menu shows the live state of the app:
    - Backend running on port 8765
    - Queue: idle / downloading, items done, pending, failed
    - Tools: yt-dlp, FFmpeg and the JavaScript runtime YouTube needs
    - Updates: your version and whether a newer one exists
* "Open web page" opens http://127.0.0.1:8765/ in your browser.
* "Stop backend" / "Start backend" control the local server.
* "Start with Windows" toggles autostart.
* "Quit Playlist Downloader" stops the server and exits.

Where things live
-----------------
Settings, queue and logs:  %APPDATA%\PlaylistDownloader
  (config.json, queue.db, app.log, launcher.log)
Downloads:                 %USERPROFILE%\Downloads\Playlist Downloader

Requirements
------------
None: the Python backend (yt-dlp, FFmpeg checks, the download queue) is
bundled inside backend\. FFmpeg itself is optional but needed for converting
audio to MP3/M4A — the web page can download it for you from Settings.

Uninstall
---------
Run uninstall.ps1, or delete %LOCALAPPDATA%\Programs\PlaylistDownloader and
the Start Menu shortcut.
