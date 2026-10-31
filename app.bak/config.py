"""User settings: defaults, validation and persistence."""

from __future__ import annotations

import json
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from . import paths

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    # Where files land
    "download_dir": str(paths.default_download_dir()),
    # A multi-track playlist gets its own folder named after the playlist, so a
    # download never scatters 1,500 files loose in the destination. A single
    # video/song is written straight into the destination as its own name.
    "make_playlist_subfolder": True,

    # ---- Audio ----
    "audio_format": "mp3",            # mp3 | m4a | opus | flac | wav | original
    "audio_quality": "320",           # kbps: 128 | 192 | 256 | 320 | best(0)
    "audio_filename": "%(artist)s - %(title)s.%(ext)s",
    "embed_cover_art": True,
    "write_cover_file": False,
    "tag_metadata": True,

    # ---- Video ----
    "video_format": "mp4",            # mp4 | mkv | webm
    "video_quality": "1080",          # best | 2160 | 1440 | 1080 | 720 | 480
    "video_filename": "%(title)s.%(ext)s",

    # ---- Behaviour ----
    "default_mode": "audio",          # audio | video for pasted YouTube links
    "rename_to_metadata": True,       # rename files to "Artist - Title"
    "quiet_logs": True,               # keep the UI log clean

    # ---- Updates ----
    "auto_check_updates": True,       # look for a newer release at launch
    "update_repo": "brianbcyang27-prog/playlist-downloader",
    "update_last_check": 0,           # unix time of the last successful check

    # ---- Performance / stability ----
    "concurrent_downloads": 3,        # 2-6; network bound, 3 is the sweet spot
    "concurrent_fragments": 4,        # parallel chunks per file
    "rate_limit_kbps": 0,             # 0 = unlimited
    "retries": 10,                    # yt-dlp-level retries inside one attempt
    "socket_timeout": 30,
    "use_archive": True,              # never re-download a finished item
    "skip_existing_files": True,
    "playlist_end": 0,                # 0 = download whole playlist

    # ---- Failure recovery ----
    "item_retries": 4,                # app-level attempts with backoff
    "retry_delay_seconds": 8,         # first backoff delay (doubles each time)
    "auto_retry_failed": True,        # cleanup pass after a run finishes
    "auto_retry_passes": 1,
    "auto_retry_cooldown": 60,        # seconds to wait before that pass

    # ---- Testing ----
    "dry_run": False,                 # resolve everything, download nothing

    # ---- Network / access ----
    "proxy": "",
    "cookies_from_browser": "",       # "" | chrome | firefox | edge | brave | safari
    "cookies_file": "",
    "sleep_between_requests": 0,      # seconds; 0 = off

    # ---- Spotify credentials (optional; enables accurate metadata) ----
    "spotify_client_id": "",
    "spotify_client_secret": "",
    # Match Spotify/text entries to YouTube with "ytsearch1: Artist - Title"
    "resolve_missing_to_youtube": True,

    # ---- Tools ----
    "ffmpeg_path": "",                # auto-detected when blank
    "ytdlp_path": "",                 # blank = use bundled python package
    "js_runtimes": [],                # blank = auto-detect (deno/node/bun/quickjs)
}

# Keys that must never be wiped by a partial settings update
_ENUMS = {
    "audio_format": {"mp3", "m4a", "opus", "flac", "wav", "original"},
    "video_format": {"mp4", "mkv", "webm"},
}

_AUDIO_QUALITIES = {"128", "192", "256", "320", "best"}
_VIDEO_QUALITIES = {"best", "2160", "1440", "1080", "720", "480", "360"}


def _coerce(value: Any, reference: Any) -> Any:
    """Coerce `value` to the type of the existing default."""
    if isinstance(reference, bool):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if isinstance(reference, int) and not isinstance(reference, bool):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return reference
    if isinstance(reference, str):
        return "" if value is None else str(value)
    return value


class Config:
    """Thread-safe JSON-backed settings store."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or paths.config_file()
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(DEFAULTS)
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            if self._path.exists():
                try:
                    stored = json.loads(self._path.read_text("utf-8"))
                    if isinstance(stored, dict):
                        self._merge(stored)
                except (OSError, json.JSONDecodeError):
                    # Corrupt config: keep defaults rather than crashing.
                    pass
            self._autodetect_tools()

    def _merge(self, stored: dict[str, Any]) -> None:
        for key, value in stored.items():
            if key in _ENUMS:
                value = str(value).lower()
                if value not in _ENUMS[key]:
                    continue
            if key == "audio_quality":
                value = str(value)
                if value not in _AUDIO_QUALITIES:
                    value = DEFAULTS["audio_quality"]
            if key == "video_quality":
                value = str(value)
                if value not in _VIDEO_QUALITIES:
                    value = DEFAULTS["video_quality"]
            if key in DEFAULTS:
                self._data[key] = _coerce(value, DEFAULTS[key])

    def save(self) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, indent=2), "utf-8")
            tmp.replace(self._path)

    # -- access ------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    # Dict-style access, so `cfg["key"]` and `cfg.get("key")` both work.
    def __getitem__(self, key: str) -> Any:
        with self._lock:
            if key not in self._data:
                raise KeyError(key)
            return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.update({key: value})

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def keys(self):
        with self._lock:
            return list(self._data.keys())

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            clean = {k: v for k, v in changes.items() if k in DEFAULTS}
            self._merge(clean)
            self._data["download_dir"] = str(
                Path(self._data["download_dir"]).expanduser()
            )
            self.save()
            return dict(self._data)

    # -- helpers -----------------------------------------------------------
    def download_dir(self) -> Path:
        path = Path(self.get("download_dir")).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _autodetect_tools(self) -> None:
        """Fill in ffmpeg / yt-dlp paths on first run."""
        from . import tools  # local import keeps config standalone
        tools.ensure_path()
        current = (self._data.get("ffmpeg_path") or "").strip()
        if not current or not Path(current).exists():
            found = shutil.which("ffmpeg")
            if found:
                self._data["ffmpeg_path"] = found
        if not self._data.get("ytdlp_path"):
            found = shutil.which("yt-dlp")
            if found:
                self._data["ytdlp_path"] = found


def tool_status(cfg: Config) -> dict[str, Any]:
    """Report which external tools are available, for the UI's health panel."""
    from . import tools  # local import avoids a cycle at module import time
    tools.ensure_path()
    configured = (cfg.get("ffmpeg_path") or "").strip()
    if configured and not Path(configured).exists():
        configured = ""
    managed_folder = tools.ffmpeg_location()
    managed = None
    if managed_folder:
        name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
        managed = str(Path(managed_folder) / name)
    ffmpeg = configured or managed or shutil.which("ffmpeg")
    ffprobe = None
    if ffmpeg:
        candidate = Path(ffmpeg).with_name("ffprobe" + (".exe" if sys.platform.startswith("win") else ""))
        if candidate.exists():
            ffprobe = str(candidate)
        else:
            ffprobe = shutil.which("ffprobe")

    try:
        import yt_dlp  # noqa: F401
        ytdlp_pkg = True
    except ImportError:
        ytdlp_pkg = False

    try:  # optional accelerator for the compare engine
        import rapidfuzz  # noqa: F401
        rapidfuzz_ok = True
    except ImportError:
        rapidfuzz_ok = False

    ytdlp_bin = cfg.get("ytdlp_path") or shutil.which("yt-dlp")
    js = available_js_runtimes()
    ejs = _has_ejs()

    return {
        "ffmpeg": ffmpeg or None,
        "ffprobe": ffprobe or None,
        "ytdlp_package": ytdlp_pkg,
        "ytdlp_binary": ytdlp_bin or None,
        "ffmpeg_ok": bool(ffmpeg),
        "ytdlp_ok": bool(ytdlp_pkg or ytdlp_bin),
        # YouTube increasingly requires a JS runtime to solve its player
        # challenge. Without one, some downloads fail with HTTP 403.
        "js_runtimes": js,
        "js_ok": bool(js),
        "ejs_installed": ejs,
        "ejs_ok": ejs or bool(js),
        # Optional: speeds the folder-vs-playlist compare up ~50x.
        "rapidfuzz_installed": rapidfuzz_ok,
    }


# ---------------------------------------------------------------------------
# Shared yt-dlp networking / environment options
# ---------------------------------------------------------------------------

_JS_CANDIDATES = ("deno", "node", "bun", "quickjs")


def available_js_runtimes() -> list[str]:
    """Which JS runtimes yt-dlp can use to solve YouTube's challenge."""
    return [name for name in _JS_CANDIDATES if shutil.which(name)]


def _has_ejs() -> bool:
    try:
        import yt_dlp_ejs  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        from yt_dlp.globals import supported_remote_components  # noqa: F401
    except Exception:
        return False
    return False


def _ffmpeg_location(cfg: Config) -> str | None:
    """Where yt-dlp should look for ffmpeg/ffprobe.

    An explicit location is important: when yt-dlp resolves the binaries via
    PATH it caches a negative result, so a download that starts before the
    one-time setup finishes would keep failing until the app is restarted.
    """
    configured = (cfg.get("ffmpeg_path") or "").strip()
    if configured and Path(configured).exists():
        return configured
    from . import tools  # local import keeps config standalone
    return tools.ffmpeg_location()


def network_opts(cfg: Config) -> dict[str, Any]:
    """Options every yt-dlp call should share: proxy, cookies, ffmpeg, JS.

    Keeping this in one place means playlist reading, single-item previews and
    the download workers all behave identically.
    """
    opts: dict[str, Any] = {}

    if cfg.get("proxy"):
        opts["proxy"] = cfg.get("proxy")
    if cfg.get("cookies_file"):
        opts["cookiefile"] = cfg.get("cookies_file")
    elif cfg.get("cookies_from_browser"):
        opts["cookiesfrombrowser"] = (cfg.get("cookies_from_browser"),)
    location = _ffmpeg_location(cfg)
    if location:
        opts["ffmpeg_location"] = location

    runtimes = cfg.get("js_runtimes") or available_js_runtimes()
    if runtimes:
        opts["js_runtimes"] = {name: {"path": None} for name in runtimes}

    # Only reach out for the external challenge solver if it is not bundled.
    if not _has_ejs():
        opts["remote_components"] = ["ejs:github"]

    return opts
