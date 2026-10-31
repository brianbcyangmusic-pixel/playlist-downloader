"""One-time download of the external binaries the app needs.

On macOS the app assumes Homebrew's ``ffmpeg`` (and a JS runtime) is already
installed, which is true on the machine this was built on and is what the
tutorial tells Mac users to do.

Windows is different: a non-technical user cannot reasonably install ffmpeg and
deno by hand, and without them the app cannot extract audio or get past
YouTube's player challenge. So on Windows the app fetches both into its own data
folder, once, and puts that folder first on ``PATH``. The downloads are ~120 MB
total but persist across launches and updates, which is why they are *not*
bundled into the exe (a one-file exe would re-extract them on every start).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

from . import __version__, paths

Progress = Callable[[dict[str, Any]], None]

# Windows static builds. Both are stable, dependency-free URLs.
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
DENO_URL = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"

# Members we keep out of each archive.
_KEEP = {"ffmpeg.exe", "ffprobe.exe", "deno.exe"}


def tools_dir() -> Path:
    path = paths.data_dir() / "tools"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_path() -> None:
    """Put the managed tools folder first on PATH (cheap and idempotent)."""
    folder = str(tools_dir())
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    if parts[:1] != [folder]:
        os.environ["PATH"] = os.pathsep.join([folder] + [p for p in parts if p and p != folder])


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform.startswith("win") else name


def ffmpeg_location() -> str | None:
    """The managed tools folder, if it really holds ffmpeg *and* ffprobe.

    yt-dlp needs both to extract audio. Passing this explicitly (instead of
    relying on PATH) also keeps its version cache keyed to a stable path.
    """
    folder = tools_dir()
    if (folder / _exe("ffmpeg")).is_file() and (folder / _exe("ffprobe")).is_file():
        return str(folder)
    return None


def reset_ytdlp_caches() -> None:
    """Forget yt-dlp's "ffmpeg not found" memory after a fresh install.

    ``FFmpegPostProcessor`` stores probe results in *class-level* dicts keyed
    by path. If any yt-dlp call ran while the tools were missing, that negative
    result sticks for the life of the process and every later conversion fails
    with "ffprobe not found" — even though ffprobe is on disk by then.
    """
    try:
        from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
    except Exception:  # yt-dlp not importable: nothing to clear
        return
    for name in ("_version_cache", "_features_cache"):
        cache = getattr(FFmpegPostProcessor, name, None)
        try:
            cache.clear()
        except (AttributeError, TypeError):
            pass


def _find(name: str) -> str | None:
    return shutil.which(name)


def status() -> dict[str, Any]:
    """What is missing, and can we fix it automatically?"""
    ensure_path()
    ffmpeg = _find("ffmpeg")
    deno = _find("deno")
    auto = sys.platform.startswith("win")
    return {
        "ffmpeg": ffmpeg,
        "deno": deno,
        "ffmpeg_ok": bool(ffmpeg),
        "js_ok": bool(deno),
        "ready": bool(ffmpeg and deno),
        "auto_install": auto,
        "folder": str(tools_dir()),
        "needed": [n for n, ok in (("FFmpeg", ffmpeg), ("Deno", deno)) if not ok],
    }


def _download(url: str, destination: Path, label: str,
              progress: Progress | None, index: int, total: int) -> Path:
    request = urllib.request.Request(url, headers={"User-Agent": f"PlaylistDownloader/{__version__}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        size = int(response.headers.get("Content-Length") or 0)
        done = 0
        with open(destination, "wb") as handle:
            while True:
                chunk = response.read(262_144)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if progress:
                    progress({
                        "stage": "downloading", "tool": label,
                        "file": index, "files": total,
                        "received": done, "total": size,
                    })
    return destination


def _extract(archive: Path, destination: Path) -> list[str]:
    """Pull just the executables we need out of a zip."""
    written: list[str] = []
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            name = Path(member.filename).name
            if name.lower() not in _KEEP:
                continue
            target = destination / name
            with bundle.open(member) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)
            target.chmod(0o755)
            written.append(name)
    return written


def install(progress: Progress | None = None, force: bool = False) -> dict[str, Any]:
    """Download and unpack anything that is missing. Safe to call twice."""
    if not sys.platform.startswith("win"):
        return {
            "ok": False,
            "error": "Automatic setup is for Windows. On macOS run: brew install ffmpeg deno",
        }

    ensure_path()
    folder = tools_dir()
    current = status()
    wanted: list[tuple[str, str, str]] = []
    if force or not current["ffmpeg_ok"]:
        wanted.append(("FFmpeg", FFMPEG_URL, "ffmpeg.zip"))
    if force or not current["js_ok"]:
        wanted.append(("Deno", DENO_URL, "deno.zip"))

    if not wanted:
        return {"ok": True, "already": True, "status": current}

    total = len(wanted)
    written: list[str] = []
    try:
        for index, (label, url, filename) in enumerate(wanted, start=1):
            if progress:
                progress({"stage": "starting", "tool": label, "file": index, "files": total,
                          "received": 0, "total": 0})
            with tempfile.TemporaryDirectory(prefix="pd-tools-") as scratch:
                archive = _download(url, Path(scratch) / filename, label, progress, index, total)
                if progress:
                    progress({"stage": "unpacking", "tool": label, "file": index, "files": total})
                written.extend(_extract(archive, folder))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "written": written}

    ensure_path()
    # Fresh binaries on PATH are not enough: purge yt-dlp's cached "not found".
    reset_ytdlp_caches()
    after = status()
    if progress:
        progress({"stage": "done", "files": total, "written": written, "ready": after["ready"]})
    return {"ok": True, "written": written, "status": after}


def summary() -> str:
    """A one-line human description, used in logs and error messages."""
    state = status()
    if state["ready"]:
        return "FFmpeg and Deno are installed."
    if not state["auto_install"]:
        return "Missing " + ", ".join(state["needed"]) + ". Install with: brew install ffmpeg deno"
    return "Missing " + ", ".join(state["needed"]) + "."
