"""Keep exactly one copy of the app running (Windows, packaged build).

The app is a single portable exe, so it is easy to end up with two copies
running — a leftover `-update.exe`, a second double-click, a stale desktop
shortcut. They all try to bind `127.0.0.1:8765`, and Windows permits several
sockets to share that port (`SO_REUSEADDR`), so the browser tab may be talking
to *either* process.

That is not cosmetic. During an update the page in the browser asked one copy
to replace itself while another copy was the one actually running, and the
staged `PlaylistDownloader-update.exe` could end up being the process the
update tried to overwrite. Windows refuses to overwrite a running exe, so the
update silently failed and left `.part` files behind.

A named mutex makes the second launch harmless: it opens the running app in the
browser and exits without touching the database, the update state or the port.
Only the packaged Windows build is affected; `python -m app.server` remains
free to run alongside it for development.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from . import paths
from .updater import pid_running

_HANDLE = None
_RUNTIME_NAME = "runtime.json"


def _mutex_name() -> str:
    digest = hashlib.sha256(str(paths.data_dir()).lower().encode("utf-8")).hexdigest()[:16]
    return f"Local\\PlaylistDownloader-{digest}"


def acquire() -> bool:
    """True when this process may serve; False when another copy already is."""
    global _HANDLE
    if not sys.platform.startswith("win") or not paths.is_frozen():
        return True
    try:
        import ctypes

        # use_last_error is required: ctypes calls in between may otherwise
        # clobber the thread's error code before we read it. The restype matters
        # too — the default (c_int) truncates a 64-bit handle on 64-bit Windows.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p,
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateMutexW(None, False, _mutex_name())
        if not handle:
            return True  # no mutex available: behave as before rather than block
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _HANDLE = handle
        return True
    except Exception:  # pragma: no cover - defensive
        return True


def publish(port: int) -> None:
    """Record where the running copy is listening, for the next launch to find."""
    if not sys.platform.startswith("win") or not paths.is_frozen():
        return
    try:
        path = paths.data_dir() / _RUNTIME_NAME
        path.write_text(json.dumps({
            "pid": os.getpid(),
            "port": int(port),
            "url": f"http://127.0.0.1:{int(port)}/",
            "started_at": time.time(),
        }), encoding="utf-8")
    except OSError:
        pass


def running_url(preferred_port: int = 8765) -> str:
    """URL of the copy that is already running, or a sensible guess."""
    fallback = f"http://127.0.0.1:{int(preferred_port)}/"
    if not sys.platform.startswith("win"):
        return fallback
    try:
        data = json.loads((paths.data_dir() / _RUNTIME_NAME).read_text("utf-8"))
        url = str(data.get("url") or "")
        if url and pid_running(int(data.get("pid") or 0)):
            return url
    except (OSError, ValueError, TypeError):
        pass
    return fallback


def clear() -> None:
    """Remove the runtime marker on a clean exit."""
    if not sys.platform.startswith("win"):
        return
    try:
        (paths.data_dir() / _RUNTIME_NAME).unlink(missing_ok=True)
    except OSError:
        pass
