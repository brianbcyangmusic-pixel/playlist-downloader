"""Self-update support.

The app asks GitHub for the newest published release, compares its tag with the
version baked into this build, and — if the user asks for it — downloads the
matching asset and swaps it in place, then relaunches.

Nothing here runs by itself: `check()` is a plain HTTPS GET, and `install()`
only acts when the UI explicitly calls it.

Asset names are produced by `.github/workflows/build-apps.yml`:

    PlaylistDownloader-Windows-x64.zip    tray app + frozen backend folder
    PlaylistDownloader-macOS.zip          zipped menu bar .app with the backend inside

Only the installed app can replace itself; from a source checkout `install()`
refuses and the UI points at the release page instead.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import __version__, paths

REPO_DEFAULT = "brianbcyangmusic-pixel/playlist-downloader"
_UA = f"PlaylistDownloader/{__version__} (+https://github.com/{REPO_DEFAULT})"

# Update payloads only ever come from a GitHub release. The URL is read out of
# the release JSON, so without this allowlist a tampered response (or a proxy)
# could point the updater at an arbitrary host — and that file is then executed
# or unpacked over the app. Hosts are matched exactly, except the CDN suffix,
# which GitHub uses for redirects.
_DOWNLOAD_HOSTS = (
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
)


def checked_download_url(url: str) -> str:
    """Return `url` if it is a GitHub release URL, else raise ValueError.

    Applied to the original URL and to every redirect hop, so a 302 cannot walk
    the download somewhere else.
    """
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise ValueError(f"Refusing update download from {parts.scheme or 'no'} URL.")
    allowed = host in _DOWNLOAD_HOSTS or host.endswith(".githubusercontent.com")
    if not allowed:
        raise ValueError(f"Refusing update download from {host}.")
    return url


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only while they stay on GitHub."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        checked_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

Progress = Callable[[dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def parse_version(text: str) -> tuple[int, ...]:
    """`v1.3` -> `(1, 3, 0)`. Unparseable input sorts below everything."""
    match = re.match(r"^\s*[vV]?(\d+(?:\.\d+)*)", str(text or ""))
    if not match:
        return (0,)
    parts = [int(p) for p in match.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(latest: str, current: str = __version__) -> bool:
    return parse_version(latest) > parse_version(current)


def current_version() -> str:
    return __version__


# ---------------------------------------------------------------------------
# Talking to GitHub
# ---------------------------------------------------------------------------

def _opener(proxy: str | None):
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def _asset_suffixes() -> tuple[str, ...]:
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return (".zip",)
    return ()


def pick_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the asset this platform can install from."""
    suffixes = _asset_suffixes()
    if not suffixes:
        return None
    usable = [
        a for a in assets
        if (a.get("name") or "").lower().endswith(suffixes)
        and a.get("browser_download_url")
    ]
    if not usable:
        return None
    token = "windows" if sys.platform.startswith("win") else "macos"
    preferred = [a for a in usable if token in (a.get("name") or "").lower()]
    return (preferred or usable)[0]


def check(cfg, timeout: float = 15.0) -> dict[str, Any]:
    """Return the update state. Never raises: failures come back as `ok: False`."""
    repo = (cfg.get("update_repo") or REPO_DEFAULT).strip("/")
    result: dict[str, Any] = {
        "ok": False,
        "current": __version__,
        "repo": repo,
        "checked_at": time.time(),
        "available": False,
        "supported": False,
        "asset": None,
    }
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    request = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/vnd.github+json",
    })
    try:
        with _opener(cfg.get("proxy") or None).open(request, timeout=timeout) as response:
            release = json.load(response)
    except urllib.error.HTTPError as exc:
        result["error"] = (
            f"GitHub returned {exc.code}. "
            + ("No published release yet." if exc.code == 404 else "Try again later.")
        )
        return result
    except Exception as exc:  # noqa: BLE001 - offline, DNS, TLS, timeouts
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    tag = str(release.get("tag_name") or "")
    asset = pick_asset(release.get("assets") or [])
    result.update({
        "ok": True,
        "latest": tag.lstrip("vV"),
        "available": is_newer(tag),
        "notes": (release.get("body") or "")[:4000],
        "html_url": release.get("html_url"),
        "published_at": release.get("published_at"),
        "supported": asset is not None and can_self_update(),
        "unsupported_reason": None if asset is None else unsupported_reason(),
        "asset": None if asset is None else {
            "name": asset.get("name"),
            "url": asset.get("browser_download_url"),
            "size": asset.get("size"),
        },
    })
    return result


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

def _download_opener(proxy: str | None):
    """Like `_opener`, but every redirect hop must still be a GitHub host."""
    handlers: list[Any] = [_CheckedRedirect()]
    if proxy:
        handlers.insert(0, urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def download(url: str, destination: Path, cfg, progress: Progress | None = None,
             timeout: float = 60.0) -> Path:
    """Stream `url` to `destination`, reporting bytes as they arrive.

    A failed or interrupted transfer leaves no `*.part` behind. That matters on
    Windows, where the download may be the thing that fails exactly because the
    destination is a running copy of the app.

    `url` must be a GitHub release URL (see `checked_download_url`): this file is
    afterwards executed (Windows) or unpacked and swapped in (macOS), so the
    source is pinned to GitHub rather than trusted because it arrived in JSON.
    """
    checked_download_url(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    tmp = destination.with_suffix(destination.suffix + ".part")
    done = 0
    try:
        with _download_opener(cfg.get("proxy") or None).open(
                request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            with open(tmp, "wb") as handle:
                while True:
                    chunk = response.read(262_144)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress({"stage": "downloading", "received": done, "total": total})
                handle.flush()
                os.fsync(handle.fileno())
        tmp.replace(destination)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return destination


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------
#
# Both releases are a zip of the whole app, and both run as a small launcher
# (Swift menu bar app / .NET tray app) that starts this backend as a child:
#
#   macOS    Playlist Downloader.app/Contents/{MacOS/PlaylistDownloader, Resources/backend/…}
#   Windows  <folder>/{PlaylistDownloader.exe, backend/PlaylistDownloaderBackend.exe, …}
#
# An update therefore replaces the *whole* app, not just this process: the
# backend downloads and unpacks the new build, starts a detached helper, and
# exits. The helper waits for the backend to go, quits the launcher, swaps the
# app in one rename (keeping the old copy until the new one is in place), and
# starts the new launcher.

MAC_LAUNCHER = "Contents/MacOS/PlaylistDownloader"
WIN_LAUNCHER = "PlaylistDownloader.exe"
WIN_BACKEND = "backend/PlaylistDownloaderBackend.exe"
MIN_ASSET_BYTES = 1_000_000
HELPER_GRACE_SECONDS = 1.0

_PYI_ENV_PREFIXES = ("_PYI_", "PYINSTALLER_")
_PYI_ENV_NAMES = {"_MEIPASS2"}


def current_app_bundle() -> Path | None:
    """The `.app` this process is running inside, if any."""
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


def install_root() -> Path | None:
    """The folder or bundle an update replaces, or None for a source checkout."""
    if sys.platform == "darwin":
        bundle = current_app_bundle()
        return bundle if bundle and (bundle / MAC_LAUNCHER).is_file() else None
    if sys.platform.startswith("win") and paths.is_frozen():
        exe = Path(sys.executable).resolve()
        root = exe.parent.parent
        if exe.parent.name.lower() == "backend" and (root / WIN_LAUNCHER).is_file():
            return root
    return None


def is_translocated(path: Path | None = None) -> bool:
    """True when macOS runs the app from a randomised read-only copy.

    Gatekeeper does this for a quarantined app opened where it was downloaded
    (e.g. straight from ~/Downloads or the Desktop). Such a copy cannot be
    replaced; moving the app into Applications ends the translocation.
    """
    target = path or current_app_bundle()
    return target is not None and "/AppTranslocation/" in str(target)


def unsupported_reason() -> str | None:
    """Why this copy cannot update itself, in words for the person using it."""
    root = install_root()
    if root is None:
        return "This copy cannot update itself. Download the new version from the website."
    if is_translocated(root):
        return ("macOS is running this copy from a temporary read-only location. "
                "Move Playlist Downloader into your Applications folder and reopen it, "
                "then updates install by themselves.")
    if not os.access(root.parent, os.W_OK):
        return f"This copy is in a folder it cannot write to ({root.parent})."
    return None


def can_self_update() -> bool:
    """Whether this copy of the app can replace itself in place."""
    return unsupported_reason() is None


def clean_child_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for the update helper and the relaunched app.

    PyInstaller's bootloader passes ``_PYI_*`` variables down to children. The
    relaunched app is a new top-level program, not a child of this one: if it
    inherits them, its own frozen backend mistakes itself for a subprocess of
    the old build and fails to start.
    """
    out = dict(os.environ if env is None else env)
    for key in list(out):
        upper = key.upper()
        if upper.startswith(_PYI_ENV_PREFIXES) or upper in _PYI_ENV_NAMES:
            out.pop(key, None)
    return out


def trusted_asset_url(url: str, cfg) -> bool:
    """True only for a release download belonging to this project.

    The install endpoint is reachable from any page in the browser (it is a
    plain localhost POST), so accepting an arbitrary address would let a
    website make the app download and run whatever it likes.

    `download()` re-checks the host on every redirect hop, because this test
    only sees the URL the page supplied, not where GitHub sends us next.
    """
    try:
        checked_download_url(url)
    except ValueError:
        return False
    repo = (cfg.get("update_repo") or REPO_DEFAULT).strip("/")
    prefix = f"https://github.com/{repo}/releases/download/"
    return str(url or "").startswith(prefix)


_MAC_HELPER = """#!/bin/sh
# Written by Playlist Downloader's updater. Waits for the backend to exit,
# quits the menu bar app, swaps the bundle and reopens it.
set -u
BACKEND_PID="$1"; NEW_APP="$2"; DEST_APP="$3"; LOG="$4"
note() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"; }
alive() { kill -0 "$1" 2>/dev/null; }
wait_gone() {  # pid, half-seconds
  i=0
  while [ "$i" -lt "$2" ] && alive "$1"; do i=$((i + 1)); sleep 0.5; done
  ! alive "$1"
}

note "update: waiting for backend $BACKEND_PID"
wait_gone "$BACKEND_PID" 120 || { kill -TERM "$BACKEND_PID" 2>/dev/null; wait_gone "$BACKEND_PID" 20; }

# The menu bar app does not quit when its backend does; quit every copy of it.
for pid in $(/usr/bin/pgrep -x PlaylistDownloader); do
  case "$(ps -o comm= -p "$pid" 2>/dev/null)" in
    */Contents/MacOS/PlaylistDownloader)
      note "update: quitting menu bar app $pid"
      kill -TERM "$pid" 2>/dev/null
      wait_gone "$pid" 20 || { kill -KILL "$pid" 2>/dev/null; wait_gone "$pid" 10; } ;;
  esac
done
sleep 0.5

STAGE="${DEST_APP}.incoming.$$"
OLD="${DEST_APP}.old.$$"
rm -rf "$STAGE"
if ! /usr/bin/ditto "$NEW_APP" "$STAGE" >>"$LOG" 2>&1; then
  note "update: could not copy the new app"; rm -rf "$STAGE"; /usr/bin/open "$DEST_APP"; exit 1
fi
if ! mv "$DEST_APP" "$OLD" >>"$LOG" 2>&1; then
  note "update: could not move the old app aside"; rm -rf "$STAGE"; /usr/bin/open "$DEST_APP"; exit 1
fi
if ! mv "$STAGE" "$DEST_APP" >>"$LOG" 2>&1; then
  note "update: could not move the new app in; restoring"; mv "$OLD" "$DEST_APP"; rm -rf "$STAGE"
  /usr/bin/open "$DEST_APP"; exit 1
fi
rm -rf "$OLD"
/usr/bin/xattr -dr com.apple.quarantine "$DEST_APP" 2>/dev/null
note "update: installed, reopening $DEST_APP"
/usr/bin/open "$DEST_APP" >>"$LOG" 2>&1
exit 0
"""

_WIN_HELPER = r"""# Written by Playlist Downloader's updater. Waits for the backend to exit,
# quits the tray app, swaps the app folder and starts the new tray app.
param([int]$BackendPid, [string]$Root, [string]$Stage, [string]$Log)
$ErrorActionPreference = 'Continue'
function Note($m) { Add-Content -LiteralPath $Log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }
$Tray = Join-Path $Root 'PlaylistDownloader.exe'

Note "update: waiting for backend $BackendPid"
Wait-Process -Id $BackendPid -Timeout 60 -ErrorAction SilentlyContinue

# Quit the tray app and anything else still running from the app folder, so
# no file in it is locked. Only processes whose image lives in $Root qualify.
$prefix = $Root.TrimEnd('\') + '\'
Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -and $_.Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) } |
  ForEach-Object { Note "update: stopping $($_.Name) $($_.Id)"; Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue; Wait-Process -Id $_.Id -Timeout 10 -ErrorAction SilentlyContinue }

$Old = "$Root.old-$PID"
$moved = $false
for ($i = 0; $i -lt 60 -and -not $moved; $i++) {
  try { Move-Item -LiteralPath $Root -Destination $Old -ErrorAction Stop; $moved = $true }
  catch { Start-Sleep -Milliseconds 500 }
}
if (-not $moved) {
  Note "update: the app folder stayed locked; keeping the current version"
  Remove-Item -LiteralPath $Stage -Recurse -Force -ErrorAction SilentlyContinue
  Start-Process -FilePath $Tray; exit 1
}
try { Move-Item -LiteralPath $Stage -Destination $Root -ErrorAction Stop }
catch {
  Note "update: could not move the new app in ($_); restoring"
  Move-Item -LiteralPath $Old -Destination $Root -ErrorAction SilentlyContinue
  Start-Process -FilePath $Tray; exit 1
}
Note "update: installed, starting $Tray"
Start-Process -FilePath $Tray -WorkingDirectory $Root
Start-Sleep -Seconds 2
Remove-Item -LiteralPath $Old -Recurse -Force -ErrorAction SilentlyContinue
exit 0
"""


def _find_new_app(unpacked: Path) -> Path | None:
    """Locate the app inside an unpacked release zip."""
    if sys.platform == "darwin":
        bundles = sorted(unpacked.glob("*.app")) or sorted(unpacked.glob("*/*.app"))
        return next((b for b in bundles if os.access(b / MAC_LAUNCHER, os.X_OK)), None)
    for folder in (unpacked, *sorted(p for p in unpacked.iterdir() if p.is_dir())):
        if (folder / WIN_LAUNCHER).is_file() and (folder / WIN_BACKEND).is_file():
            return folder
    return None


def _unpack(payload: Path, target: Path) -> None:
    """Extract a release zip. macOS needs `ditto`: Python's zipfile drops the
    executable bits and symlinks that an app bundle depends on."""
    target.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(payload), str(target)],
                       check=True, capture_output=True)
        return
    with zipfile.ZipFile(payload) as archive:
        for member in archive.namelist():
            resolved = (target / member).resolve()
            if target.resolve() not in resolved.parents and resolved != target.resolve():
                raise ValueError(f"Unsafe path in update archive: {member}")
        archive.extractall(target)


def _spawn_helper(root: Path, new_app: Path, work: Path, log: Path) -> subprocess.Popen:
    env = clean_child_environment()
    if sys.platform == "darwin":
        helper = work / "apply-update.sh"
        helper.write_text(_MAC_HELPER, encoding="utf-8")
        helper.chmod(helper.stat().st_mode | stat.S_IEXEC)
        return subprocess.Popen(
            ["/bin/sh", str(helper), str(os.getpid()), str(new_app), str(root), str(log)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, cwd=str(work), env=env)
        return
    helper = work / "apply-update.ps1"
    helper.write_text(_WIN_HELPER, encoding="utf-8")
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-WindowStyle", "Hidden", "-File", str(helper),
         "-BackendPid", str(os.getpid()), "-Root", str(root),
         "-Stage", str(new_app), "-Log", str(log)],
        cwd=str(work), env=env, close_fds=True, creationflags=flags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install(asset_url: str, cfg, progress: Progress | None = None) -> dict[str, Any]:
    """Download and unpack the new build, then hand over to the swap helper.

    Returns `{"ok": bool, ...}`. On success the caller must exit this process:
    the helper waits for it before touching any file.
    """
    root = install_root()
    reason = unsupported_reason()
    if root is None or reason:
        return {"ok": False, "error": reason or "This copy cannot update itself."}

    log = paths.data_dir() / "update.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    # On Windows the new build is unpacked beside the app, so the final swap is
    # a rename on one volume rather than a slow copy that could fail halfway.
    work_parent = root.parent if sys.platform.startswith("win") else None
    work = Path(tempfile.mkdtemp(prefix=".pd-update-", dir=work_parent))
    try:
        if progress:
            progress({"stage": "downloading", "received": 0, "total": 0})
        payload = download(asset_url, work / "update.zip", cfg, progress)
        size = payload.stat().st_size
        if size < MIN_ASSET_BYTES:
            raise ValueError("The downloaded update looks incomplete. Try again.")
        if progress:
            progress({"stage": "preparing", "received": size, "total": size})

        _unpack(payload, work / "unpacked")
        payload.unlink(missing_ok=True)
        new_app = _find_new_app(work / "unpacked")
        if new_app is None:
            raise ValueError("The update archive did not contain the app.")

        helper = _spawn_helper(root, new_app, work, log)
        # The helper waits for this process, so it cannot have finished yet;
        # an early exit means it never started and the app must keep running.
        time.sleep(HELPER_GRACE_SECONDS)
        if helper.poll() is not None:
            raise RuntimeError(f"The update helper stopped early (exit {helper.returncode}). See {log}.")
        if progress:
            progress({"stage": "restarting"})
        return {"ok": True, "restarting": True, "log": str(log)}
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(work, ignore_errors=True)
        detail = exc.stderr.decode(errors="replace").strip() if isinstance(
            exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {detail}"}


def pid_running(pid: int) -> bool:
    """True if `pid` is alive. (os.kill(pid, 0) would *kill* on Windows.)"""
    if sys.platform.startswith("win"):
        try:
            import ctypes

            # A default ctypes restype is c_int, which truncates handles on
            # 64-bit Windows; be explicit about every signature we use.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = [
                ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong,
            ]
            kernel32.GetExitCodeProcess.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
            ]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFO
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:  # pragma: no cover
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def cleanup_stale_update(background: bool = True) -> None:
    """Remove leftovers of an earlier update: the old copy the helper could not
    delete yet, and unpacked downloads from an attempt that was abandoned."""
    root = install_root()
    if root is None:
        return
    patterns = ((f"{root.name}.old-*", ".pd-update-*") if sys.platform.startswith("win")
                else (f"{root.name}.old.*", f"{root.name}.incoming.*"))

    def sweep() -> None:
        for _ in range(20):
            leftovers = [p for pattern in patterns for p in root.parent.glob(pattern)]
            if not leftovers:
                return
            for path in leftovers:
                shutil.rmtree(path, ignore_errors=True)
            time.sleep(1.5)

    if background:
        threading.Thread(target=sweep, daemon=True, name="update-cleanup").start()
    else:
        sweep()


def platform_label() -> str:
    if sys.platform.startswith("win"):
        return f"Windows {platform.machine() or 'x64'}"
    if sys.platform == "darwin":
        return f"macOS {platform.machine() or 'arm64'}"
    return sys.platform
