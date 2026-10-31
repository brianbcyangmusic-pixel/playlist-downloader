"""Cross-platform locations for config, database, logs and the download archive.

Everything the app writes lives inside one per-user folder so it can be copied,
backed up or deleted as a unit:

    macOS    ~/Library/Application Support/PlaylistDownloader
    Windows  %APPDATA%\\PlaylistDownloader
    Linux    ~/.config/PlaylistDownloader
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def app_root() -> Path:
    """Directory that contains the `web/` folder and default config."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Per-user writable data directory."""
    override = os.environ.get("PLAYLIST_DOWNLOADER_HOME")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "PlaylistDownloader"
    elif sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        path = Path(base) / "PlaylistDownloader"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
        path = Path(base) / "PlaylistDownloader"

    path.mkdir(parents=True, exist_ok=True)
    return path


def config_file() -> Path:
    return data_dir() / "config.json"


def database_file() -> Path:
    return data_dir() / "queue.db"


def archive_file() -> Path:
    return data_dir() / "download-archive.txt"


def log_file() -> Path:
    return data_dir() / "app.log"


def cache_dir() -> Path:
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def thumb_dir() -> Path:
    """Scratch space for thumbnails on their way into the file's tags."""
    path = cache_dir() / "thumbs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def web_dir() -> Path:
    return app_root() / "web"


def default_download_dir() -> Path:
    """A sensible default output folder (Music / Videos under the home dir)."""
    for candidate in ("Music", "Videos", "Downloads"):
        folder = Path.home() / candidate
        if folder.is_dir():
            return Path.home() / "Downloads" / "Playlist Downloader"
    return Path.home() / "Downloads" / "Playlist Downloader"
