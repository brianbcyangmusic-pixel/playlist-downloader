"""Self-update support.

The app asks GitHub for the newest published release, compares its tag with the
version baked into this build, and — if the user asks for it — downloads the
matching asset and swaps it in place, then relaunches.

Nothing here runs by itself: `check()` is a plain HTTPS GET, and `install()`
only acts when the UI explicitly calls it.

Asset names are produced by `.github/workflows/release.yml`:

    PlaylistDownloader-<version>-windows-x64.exe    Windows, single portable exe
    PlaylistDownloader-<version>-macos.zip          macOS, zipped .app (for updates)
    Playlist Downloader <version>.dmg               macOS, for humans

A frozen build is required to self-replace; from a source checkout `install()`
refuses and points at the release page instead.
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

REPO_DEFAULT = "brianbcyang27-prog/playlist-downloader"
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
    if sys.platform.startswith("win"):
        return (".exe",)
    if sys.platform == "darwin":
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

def current_app_bundle() -> Path | None:
    """The `.app` this process is running inside, if any.

    Note the macOS build is *not* a PyInstaller freeze: it ships a real CPython
    inside ``Contents/Resources``. So the bundle is found by walking up from the
    interpreter, not by asking whether we are frozen.
    """
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


def can_self_update() -> bool:
    """Whether this copy of the app can replace itself in place."""
    if sys.platform.startswith("win"):
        return paths.is_frozen()
    if sys.platform == "darwin":
        return current_app_bundle() is not None
    return False


def _tail(path: Path, lines: int = 40) -> str:
    try:
        return "\n".join(path.read_text("utf-8", "replace").splitlines()[-lines:])
    except OSError:
        return ""


_MAC_HELPER = """#!/bin/sh
# Written by Playlist Downloader's updater. Waits for the old app to exit, then
# swaps the bundle and reopens it.
set -u
WATCH_PID="$1"; NEW_APP="$2"; DEST_APP="$3"; LOG="$4"

i=0
while [ "$i" -lt 240 ]; do
  kill -0 "$WATCH_PID" 2>/dev/null || break
  i=$((i + 1)); sleep 1
done
sleep 1

STAGE="${DEST_APP}.incoming.$$"
rm -rf "$STAGE"
if /usr/bin/ditto "$NEW_APP" "$STAGE" >>"$LOG" 2>&1; then
  rm -rf "$DEST_APP"
  if mv "$STAGE" "$DEST_APP" >>"$LOG" 2>&1; then
    /usr/bin/open "$DEST_APP" >>"$LOG" 2>&1
    exit 0
  fi
fi
rm -rf "$STAGE"
echo "update failed, see $LOG" >>"$LOG"
exit 1
"""

_WIN_HELPER = None  # replaced by the --apply-update mode below (no cmd.exe quoting traps)

# Files the Windows updater creates next to the app. Everything with this prefix
# is ours, so it is safe to sweep away on the next launch.
STAGED_EXE_PREFIX = "PlaylistDownloader-update"
STAGED_EXE_NAME = STAGED_EXE_PREFIX + ".exe"  # name used by 1.4.x, still cleaned up
_STAGE_SERIAL = 0

_PYI_ENV_PREFIXES = ("_PYI_", "PYINSTALLER_")
_PYI_ENV_NAMES = {"_MEIPASS2"}


def clean_child_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a newly spawned copy of the app.

    PyInstaller's onefile bootloader hands its extraction directory down to
    children through ``_PYI_*`` variables. The staged update exe and the
    relaunched app are *new* top-level programs, not children of the archive
    that is running now: if they inherit the old temp directory, the old
    process deletes it on exit and the new one loses the DLLs it is running
    from (it either dies immediately or never starts at all).
    """
    out = dict(os.environ if env is None else env)
    for key in list(out):
        upper = key.upper()
        if upper.startswith(_PYI_ENV_PREFIXES) or upper in _PYI_ENV_NAMES:
            out.pop(key, None)
    return out


def staged_exe_path(destination: Path) -> Path:
    """A download path that can never be the executable we are running from.

    1.4.x always downloaded to ``PlaylistDownloader-update.exe``. That is fine
    until the user starts that leftover file — it is a full copy of the app —
    and then asks it to update: the destination is *itself*, and Windows
    refuses to replace a running exe with "Access is denied". A unique name per
    attempt removes that whole class of failure.
    """
    global _STAGE_SERIAL
    destination = destination.resolve()
    for _ in range(20):
        _STAGE_SERIAL += 1
        candidate = destination.with_name(
            f"{STAGED_EXE_PREFIX}-{os.getpid()}-{_STAGE_SERIAL}.exe")
        if candidate != destination and not candidate.exists():
            return candidate
        time.sleep(0.01)
    # Practically unreachable: a running process cannot own every number.
    return destination.with_name(
        f"{STAGED_EXE_PREFIX}-{os.getpid()}-{time.time_ns()}.exe")


def trusted_asset_url(url: str, cfg) -> bool:
    """True only for a release download belonging to this project.

    The install endpoint is reachable from any page in the browser (it is a
    plain localhost POST), so accepting an arbitrary address would let a
    website make the app download and execute whatever it likes.

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



def install(asset_url: str, cfg, progress: Progress | None = None) -> dict[str, Any]:
    """Download and swap in the new build. Returns `{"ok": bool, ...}`."""
    if not can_self_update():
        return {"ok": False, "error": "Self-update only works in a packaged build."}

    if sys.platform.startswith("win"):
        destination = Path(sys.executable).resolve()
        asset_name = "update.exe"
    elif sys.platform == "darwin":
        destination = current_app_bundle()  # type: ignore[assignment]
        asset_name = "update.zip"
    else:
        return {"ok": False, "error": "Self-update is not supported on this platform."}

    if destination is None:
        return {"ok": False, "error": "Could not locate the installed app to replace."}

    work = Path(tempfile.mkdtemp(prefix="pd-update-"))
    log = paths.data_dir() / "update.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform.startswith("win"):
            # Write the new build beside the old one and let *it* do the swap.
            # That avoids running a .cmd through cmd.exe, whose quote handling
            # mangles paths that contain spaces (very common under %TEMP%).
            staged = staged_exe_path(destination)
            if progress:
                progress({"stage": "downloading", "received": 0, "total": 0})
            download(asset_url, staged, cfg, progress)
            size = staged.stat().st_size
            if size < 1_000_000:
                staged.unlink(missing_ok=True)
                return {"ok": False,
                        "error": "The downloaded update looks incomplete. Try again."}
            if progress:
                progress({"stage": "preparing", "received": size, "total": size})
            helper = subprocess.Popen(
                [str(staged), "--apply-update", str(destination), str(os.getpid())],
                cwd=str(destination.parent),
                close_fds=True,
                env=clean_child_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # The helper cannot finish while this process is alive (it waits
            # for our pid), so an immediate exit here means it never started.
            time.sleep(1.0)
            code = helper.poll()
            if code not in (None, 0):
                return {"ok": False,
                        "error": f"The downloaded update could not start (exit {code}). "
                                 f"See {log}."}
            if progress:
                progress({"stage": "restarting"})
            return {"ok": True, "restarting": True, "log": str(log)}

        if progress:
            progress({"stage": "downloading", "received": 0, "total": 0})
        payload = work / asset_name
        download(asset_url, payload, cfg, progress)
        if progress:
            progress({"stage": "preparing", "received": payload.stat().st_size,
                      "total": payload.stat().st_size})

        with zipfile.ZipFile(payload) as archive:
            archive.extractall(work / "unpacked")
        bundles = sorted((work / "unpacked").rglob("*.app"))
        if not bundles:
            return {"ok": False, "error": "The update archive did not contain an app."}
        new_app = bundles[0]
        helper = work / "apply-update.sh"
        helper.write_text(_MAC_HELPER, encoding="utf-8")
        helper.chmod(helper.stat().st_mode | stat.S_IEXEC)
        # Wait for the whole app (the Swift shell is our parent), not just this
        # backend process, so the bundle is not swapped underneath it.
        watch = os.getppid()
        command = ["/bin/sh", str(helper), str(watch), str(new_app), str(destination), str(log)]

        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(work))
        if progress:
            progress({"stage": "restarting"})
        return {"ok": True, "restarting": True, "log": str(log)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Windows: second stage, run by the freshly downloaded exe
# ---------------------------------------------------------------------------

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


def _wait_for_exit(pid: int, timeout: float) -> bool:
    """Wait until `pid` is gone (or the timeout elapses)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_running(pid):
            return True
        time.sleep(0.25)
    return not pid_running(pid)


def _replace_file(source: Path, target: Path, attempts: int = 240) -> tuple[bool, str]:
    """Copy `source` beside `target`, then swap it in with an atomic rename.

    Windows keeps the image of a running exe open, so a direct write fails with
    a sharing violation until the last process started from it has exited. If
    the copy itself were the target, a failure halfway through would leave a
    truncated application behind; building it next to the target and renaming
    means the old exe is either untouched or fully replaced, never half-written.
    """
    staging = target.with_name(f"{target.name}.new-{os.getpid()}")
    expected = source.stat().st_size
    last = "unknown error"
    try:
        for attempt in range(attempts):
            try:
                if not staging.exists():
                    try:
                        shutil.copy2(source, staging)
                    except BaseException:
                        # Never leave a truncated copy behind: the next attempt
                        # would happily swap that half-written file in.
                        staging.unlink(missing_ok=True)
                        raise
                if staging.stat().st_size != expected:
                    staging.unlink(missing_ok=True)
                    raise OSError("copy was incomplete")
                os.replace(staging, target)
                return True, f"attempt {attempt + 1}"
            except OSError as exc:
                last = str(exc)
                time.sleep(0.25)
        return False, last
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass


def apply_update(argv: list[str]) -> int | None:
    """`--apply-update OLD_EXE PID` — swap ourselves over the old build, relaunch.

    Returns None when the flag is absent (normal launch). This runs before the
    server starts, so no port is bound and nothing is downloaded.
    """
    if "--apply-update" not in argv:
        return None
    index = argv.index("--apply-update")
    try:
        old_exe = Path(argv[index + 1]).resolve()
        old_pid = int(argv[index + 2])
    except (IndexError, ValueError):
        return 2

    log = paths.data_dir() / "update.log"
    log.parent.mkdir(parents=True, exist_ok=True)

    def note(message: str) -> None:
        try:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass

    note(f"apply-update: waiting for pid {old_pid} ({sys.executable} -> {old_exe})")
    if not _wait_for_exit(old_pid, 300):
        note("apply-update: the old app never exited; leaving it alone")
        return 1
    time.sleep(0.5)

    ok, detail = _replace_file(Path(sys.executable).resolve(), old_exe)
    if not ok:
        note(f"apply-update: could not replace the app: {detail}")
        return 1
    note(f"apply-update: replaced {old_exe} ({detail})")

    error = "unknown error"
    for attempt in range(20):
        try:
            child = subprocess.Popen(
                [str(old_exe)],
                cwd=str(old_exe.parent),
                close_fds=True,
                env=clean_child_environment(),
            )
            note(f"apply-update: relaunched (pid {child.pid}, attempt {attempt + 1})")
            time.sleep(1.5)
            if child.poll() is not None:
                # Exit 0 here means it found another copy already running,
                # which is a perfectly fine outcome after an update.
                note(f"apply-update: new app exited with {child.returncode}")
            return 0
        except OSError as exc:
            error = str(exc)
            time.sleep(0.5)
    note(f"apply-update: relaunch failed: {error}")
    return 1


def _is_stale_update_file(path: Path, exe: Path) -> bool:
    """Only ever touch files this updater creates, never anything else."""
    try:
        if path == exe or not path.is_file():
            return False
    except OSError:
        return False
    name = path.name
    if name.startswith(exe.name + ".new-"):
        return True
    # Stage files are always "<prefix>.exe" (1.4.x) or "<prefix>-<pid>-<n>.exe"
    # plus their ".part" downloads. Requiring the suffix keeps unrelated files
    # that merely start with the same word out of scope.
    return (
        name.startswith(STAGED_EXE_PREFIX)
        and (name.endswith(".exe") or name.endswith(".exe.part"))
    )


def cleanup_stale_update(background: bool = True) -> None:
    """Remove leftovers from this and older self-updates.

    A staged exe cannot delete itself, and the freshly relaunched app normally
    starts *before* that helper has finished exiting, so the first attempt is
    expected to fail. 1.4.x tried exactly once and gave up, which is why
    `PlaylistDownloader-update.exe`, its `.part` and `.new-*` siblings were left
    sitting next to the app forever. Retry in the background instead.
    """
    if not sys.platform.startswith("win") or not paths.is_frozen():
        return
    exe = Path(sys.executable).resolve()
    folder = exe.parent

    def sweep() -> None:
        for _ in range(40):
            leftovers = []
            try:
                leftovers = [p for p in folder.iterdir() if _is_stale_update_file(p, exe)]
            except OSError:
                return
            if not leftovers:
                return
            for path in leftovers:
                try:
                    path.unlink()
                except OSError:
                    pass  # still mapped by a helper that is exiting
            time.sleep(0.5)

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
