"""HTTP API + static file server (standard library only, no web framework).

Endpoints
---------
GET    /                              web UI
GET    /api/state                     settings, tool health, queue stats
GET    /api/events                    Server-Sent Events progress stream
POST   /api/settings                  update settings
GET    /api/playlists                 list playlists
GET    /api/playlists/<id>/items      items in a playlist
DELETE /api/playlists/<id>            remove a playlist and its queue
POST   /api/playlist/import           import a URL / file (background job)
POST   /api/download/start|pause|resume|stop
POST   /api/items/<id>/retry          re-queue one item
POST   /api/items/retry-failed        re-queue all failures
GET    /api/browse                    browse folders server-side
POST   /api/compare                   compare a folder to a playlist (bg job)
GET    /api/job/<id>                  fetch a finished job's result
POST   /api/compare/import-missing    queue the missing tracks
POST   /api/shutdown                  quit the app
"""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse, urlsplit

from . import APP_NAME, __version__, paths, playlist as pl, tools, updater
from .cloud import Cloud
from .config import Config, DEFAULTS, tool_status
from .db import QueueDB
from .downloader import DownloadManager
from .events import EventHub, get_hub
from .matcher import compare_playlist_to_folder
from .telemetry import Telemetry

MAX_BODY = 4 * 1024 * 1024


class JobStore:
    """In-memory results for background jobs (import / compare)."""

    def __init__(self, ttl: float = 3600) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._ttl = ttl

    def create(self, kind: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id, "kind": kind, "status": "running",
                "progress": 0, "message": "", "result": None,
                "error": None, "started": time.time(),
            }
        self._gc()
        return job_id

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(fields)

    def finish(self, job_id: str, result: Any) -> None:
        self.update(job_id, status="done", result=result, progress=100)

    def fail(self, job_id: str, error: str) -> None:
        self.update(job_id, status="error", error=error)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def _gc(self) -> None:
        cutoff = time.time() - self._ttl
        with self._lock:
            self._jobs = {
                k: v for k, v in self._jobs.items() if v["started"] > cutoff
            }


class AppContext:
    """Shared singletons wired up by server.py."""

    def __init__(self) -> None:
        self.cfg = Config()
        self.db = QueueDB()
        self.hub: EventHub = get_hub()
        self.manager = DownloadManager(self.cfg, self.db, self.hub)
        self.jobs = JobStore()
        self.shutdown = threading.Event()
        # Accounts are optional: without a cloud_config.json the app runs
        # exactly as before, with no sign-in screen and no logging.
        self.cloud = Cloud()
        self.telemetry = Telemetry(self.cloud, self.db)
        self.telemetry.start(self.hub)
        # Sign-in handoffs: state -> (created_at). One per browser attempt.
        self._pending_signin: dict[str, float] = {}
        self._pending_lock = threading.Lock()


CTX: AppContext | None = None


def _cached_update_check(ctx: AppContext, force: bool = False,
                         max_age: float = 24 * 3600) -> dict[str, Any]:
    """A launch-time check that hits GitHub at most once a day."""
    last = float(ctx.cfg.get("update_last_check") or 0)
    if not force and last and (time.time() - last) < max_age:
        cached = getattr(ctx, "_update_state", None)
        if cached:
            return cached
    state = updater.check(ctx.cfg)
    if state.get("ok"):
        ctx._update_state = state  # type: ignore[attr-defined]
        try:
            ctx.cfg.update({"update_last_check": state["checked_at"]})
        except Exception:
            pass
    return state


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

def _run_import(ctx: AppContext, job_id: str, source: str, location: str,
                mode: str = "audio", autostart: bool = False) -> None:
    hub = ctx.hub
    try:
        hub.emit("import_progress", {"job": job_id, "stage": "reading",
                                     "message": f"Reading playlist from {source}…"})
        name, kind, tracks, warning = pl.fetch_playlist(source, location, ctx.cfg)

        if not tracks:
            raise ValueError("That playlist appears to be empty.")

        # Tracks without a URL get a YouTube search query so the worker pool can
        # resolve them in parallel at download time.
        items = []
        for t in tracks:
            url = t.url
            if not url and ctx.cfg.get("resolve_missing_to_youtube", True):
                q = t.search_query or t.title
                url = f"ytsearch1:{q}"
            if not url:
                continue
            items.append({
                "url": url,
                "source": kind,
                "title": t.title,
                "artist": t.artist,
                "album": t.album,
                "duration": t.duration,
                "thumbnail": t.thumbnail,
                "track_number": t.track_number,
                "mode": mode,
            })

        if not items:
            raise ValueError("Nothing importable was found in that playlist.")

        playlist_id = ctx.db.add_playlist(name, kind, location)
        ctx.db.add_items(playlist_id, items)
        total = ctx.db.count_items(playlist_id)

        hub.emit("import_progress", {
            "job": job_id, "stage": "done",
            "message": f"Imported {total} tracks from “{name}”.",
            "count": total,
            "warning": warning,
        })
        ctx.jobs.finish(job_id, {
            "playlist_id": playlist_id, "name": name, "source": kind,
            "count": total, "warning": warning,
        })
        hub.emit("playlists_changed", {})

        # One-click flow: import and immediately begin downloading.
        if autostart:
            started = ctx.manager.start(playlist_id=playlist_id)
            hub.emit("import_progress", {
                "job": job_id, "stage": "started",
                "message": ("Download started." if started.get("ok")
                            else f"Could not start: {started.get('error')}"),
                "started": bool(started.get("ok")),
            })
    except Exception as exc:
        ctx.jobs.fail(job_id, f"{type(exc).__name__}: {exc}")
        ctx.telemetry.report_error(
            f"{type(exc).__name__}: {exc}", kind="import_failed",
            link=location, source=source)
        hub.emit("import_progress", {"job": job_id, "stage": "error",
                                     "message": f"{type(exc).__name__}: {exc}"})


def _run_compare(ctx: AppContext, job_id: str, tracks: list[dict], folder: str,
                 threshold: float, recursive: bool, read_tags: bool,
                 label: str) -> None:
    hub = ctx.hub
    try:
        hub.emit("compare_progress", {"job": job_id, "stage": "scanning",
                                      "message": f"Scanning {folder}…"})
        result = compare_playlist_to_folder(
            tracks, folder, threshold=threshold,
            recursive=recursive, read_tags=read_tags,
        )
        hub.emit("compare_progress", {"job": job_id, "stage": "done",
                                      "message": "Comparison complete."})
        ctx.jobs.finish(job_id, {
            "label": label,
            "folder": folder,
            "threshold": threshold,
            "counts": result.counts,
            "matched": result.matched,
            "missing": result.missing,
            "extra": result.extra,
        })
    except Exception as exc:
        ctx.jobs.fail(job_id, f"{type(exc).__name__}: {exc}")
        ctx.telemetry.report_error(
            f"{type(exc).__name__}: {exc}", kind="compare_failed", folder=folder)
        hub.emit("compare_progress", {"job": job_id, "stage": "error",
                                      "message": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Local-only request guards
# ---------------------------------------------------------------------------

_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}


def _host_name(raw: str) -> str:
    """Hostname out of a Host header or an Origin URL, without the port."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if "//" not in text:  # Host is "host:port"; Origin is "scheme://host"
        text = "//" + text
    try:
        return (urlsplit(text).hostname or "").lower()
    except ValueError:
        return ""


# Extensions the app ships. A constant table means the Content-Type header is
# never derived from the request, so a crafted filename cannot inject a header.
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".webmanifest": "application/manifest+json",
}

_WEB_ASSETS: dict[Path, dict[str, Path]] = {}


def _web_assets(root: Path) -> dict[str, Path]:
    """Map of `relative/posix/path` -> file, built by walking `root`.

    Static serving looks the request up in this map instead of joining it onto
    `root`, so traversal is impossible by construction: a name that is not a key
    is simply not served. Keys are built from the filesystem, never from the
    request.
    """
    cached = _WEB_ASSETS.get(root)
    if cached is None:
        cached = {
            item.relative_to(root).as_posix(): item
            for item in root.rglob("*") if item.is_file()
        }
        _WEB_ASSETS[root] = cached
    return cached


def _content_type(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _local_path(raw: str) -> Path:
    """Resolve a user-supplied path, rejecting control characters.

    These routes exist so the person using the app can point it at their own
    folders. The boundary is that the server only listens on loopback and only
    accepts requests addressed to loopback (`_request_is_local`); the path is
    the feature, not an injection.
    """
    text = str(raw or "").strip()
    if not text or "\x00" in text or any(ord(ch) < 32 for ch in text):
        raise ValueError("Invalid path.")
    return Path(text).expanduser().resolve()


class Handler(BaseHTTPRequestHandler):
    server_version = f"PlaylistDownloader/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):  # keep the console readable
        if "/api/events" in (self.path or ""):
            return
        ctx = CTX
        if ctx is not None:
            ctx.hub.emit("log", {"level": "http", "message": fmt % args})

    def _request_is_local(self) -> bool:
        """Reject DNS-rebinding and cross-site requests.

        A rebinding attack reaches 127.0.0.1 through an attacker-controlled
        hostname, so the Host header is the attacker's domain. Browsers also
        attach Origin to cross-site requests. Requiring both to name an address
        this server actually answers on means only pages it handed out can call
        it.

        `--host` is honoured: a LAN bind accepts that address too, and binding
        all interfaces (`0.0.0.0`) is an explicit choice to be reachable, so the
        Host half is moot there — the Origin half still applies.
        """
        server = getattr(self, "server", None)
        bound = str(getattr(server, "server_address", ("",))[0] or "").lower()
        open_bind = bound in ("0.0.0.0", "::")
        names = set(_LOOPBACK_NAMES)
        if bound and not open_bind:
            names.add(bound)

        if not open_bind and _host_name(self.headers.get("Host") or "") not in names:
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if origin and _host_name(origin) not in names:
            return False
        return True

    def _ctx(self) -> AppContext:
        assert CTX is not None
        return CTX

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("Request body too large.")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Invalid JSON body.")

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        try:
            self._route("GET")
        except Exception as exc:
            self._crash(exc)

    def do_POST(self):
        try:
            self._route("POST")
        except Exception as exc:
            self._crash(exc)

    def do_DELETE(self):
        try:
            self._route("DELETE")
        except Exception as exc:
            self._crash(exc)

    def _crash(self, exc: Exception) -> None:
        traceback.print_exc()
        ctx = CTX
        if ctx is not None:
            try:
                ctx.telemetry.report_error(
                    f"{type(exc).__name__}: {exc}", kind="app_error",
                    path=self.path, method=self.command,
                    trace=traceback.format_exc()[-1500:])
            except Exception:
                pass
        try:
            self._error(f"{type(exc).__name__}: {exc}", 500)
        except Exception:
            pass

    def _route(self, method: str) -> None:
        if not self._request_is_local():
            return self._error("Forbidden.", 403)
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if method == "GET":
            if path == "/":
                return self._serve_static("index.html")
            if path == "/api/state":
                return self._api_state()
            if path == "/api/events":
                return self._api_events()
            if path == "/api/playlists":
                return self._json({"ok": True, "playlists": self._ctx().db.list_playlists()})
            if path.startswith("/api/playlists/") and path.endswith("/items"):
                pid = int(path.split("/")[3])
                items = self._ctx().db.get_items(pid, limit=100000)
                return self._json({"ok": True, "items": items})
            if path.startswith("/api/job/"):
                job = self._ctx().jobs.get(path.split("/")[3])
                if not job:
                    return self._error("Unknown job.", 404)
                return self._json({"ok": True, "job": job})
            if path == "/api/browse":
                return self._api_browse(query.get("path", [""])[0])
            if path == "/api/update/check":
                return self._api_update_check(query)
            if path == "/api/tools/status":
                return self._json({"ok": True, **tools.status()})
            if path == "/api/ping":
                return self._json({"ok": True, "version": __version__})
            if path == "/api/auth/state":
                return self._api_auth_state()
            if path == "/auth/handoff":
                return self._serve_static("handoff.html")
            # static assets
            return self._serve_static(path.lstrip("/"))

        if method == "POST":
            if path == "/api/auth/signup":
                return self._api_auth_signup()
            if path == "/api/auth/signin":
                return self._api_auth_signin()
            if path == "/api/auth/google":
                return self._api_auth_google()
            if path == "/api/auth/signout":
                return self._json({"ok": True, **self._ctx().cloud.sign_out()})
            if path == "/api/auth/reset":
                return self._api_auth_reset()
            if path == "/api/auth/handoff":
                return self._api_auth_handoff()
            if path == "/api/settings":
                return self._api_settings()
            if path == "/api/playlist/import":
                return self._api_import()
            if path == "/api/download/start":
                return self._api_download_start()
            if path == "/api/update/install":
                return self._api_update_install()
            if path == "/api/tools/install":
                return self._api_tools_install()
            if path == "/api/download/pause":
                return self._json({"ok": True, **self._ctx().manager.pause()})
            if path == "/api/download/resume":
                return self._json({"ok": True, **self._ctx().manager.resume()})
            if path == "/api/download/stop":
                return self._json({"ok": True, **self._ctx().manager.stop()})
            if path == "/api/items/retry-failed":
                if not self._require_auth():
                    return
                body = self._read_json()
                n = self._ctx().db.retry_failed(body.get("playlist_id"))
                return self._json({"ok": True, "requeued": n})
            if path.startswith("/api/items/") and path.endswith("/retry"):
                if not self._require_auth():
                    return
                iid = int(path.split("/")[3])
                if not self._ctx().db.get_item(iid):
                    return self._error(f"No such item: {iid}", 404)
                self._ctx().db.mark_pending(iid)
                return self._json({"ok": True, "item_id": iid})
            if path == "/api/compare":
                return self._api_compare()
            if path == "/api/compare/import-missing":
                return self._api_import_missing()
            if path == "/api/reveal":
                return self._api_reveal()
            if path == "/api/shutdown":
                threading.Thread(target=self._shutdown_soon, daemon=True).start()
                return self._json({"ok": True})

        if method == "DELETE":
            if path.startswith("/api/playlists/"):
                pid = int(path.split("/")[3])
                self._ctx().db.delete_playlist(pid)
                self._ctx().hub.emit("playlists_changed", {})
                return self._json({"ok": True})
            if path.startswith("/api/items/"):
                iid = int(path.split("/")[3])
                self._ctx().db.update_item(iid, status="skipped")
                return self._json({"ok": True})

        self._error("Not found.", 404)

    # -- static files ------------------------------------------------------
    def _serve_static(self, rel: str) -> None:
        root = paths.web_dir().resolve()
        target = _web_assets(root).get(str(rel).lstrip("/"))
        if target is None:
            return self._error("Not found.", 404)
        data = target.read_bytes()
        ctype = _content_type(target)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # -- API: state --------------------------------------------------------
    def _api_state(self) -> None:
        ctx = self._ctx()
        self._json({
            "ok": True,
            "version": __version__,
            "app_name": APP_NAME,
            "settings": ctx.cfg.as_dict(),
            "defaults": DEFAULTS,
            "tools": {**tool_status(ctx.cfg), "setup": tools.status()},
            "stats": ctx.db.stats(),
            "run": ctx.manager.status(),
            "download_dir": str(ctx.cfg.download_dir()),
            "data_dir": str(paths.data_dir()),
            "platform": __import__("sys").platform,
            "auth": ctx.cloud.state(),
        })

    def _api_settings(self) -> None:
        ctx = self._ctx()
        changes = self._read_json()
        updated = ctx.cfg.update(changes)
        ctx.hub.emit("settings_changed", {"settings": updated})
        self._json({"ok": True, "settings": updated,
                    "tools": tool_status(ctx.cfg)})

    # -- API: accounts -----------------------------------------------------
    def _require_auth(self) -> bool:
        """True when the request may proceed. Sends a 401 when it may not."""
        ctx = self._ctx()
        if not ctx.cloud.enabled:
            return True
        if ctx.cloud.state().get("signed_in"):
            return True
        # The body is still unread; discard it or the next request on this
        # keep-alive connection would be parsed from the middle of it.
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= MAX_BODY:
                self.rfile.read(length)
        except (ValueError, OSError):
            pass
        self._error("Please sign in first.", 401)
        return False

    def _api_auth_state(self) -> None:
        ctx = self._ctx()
        payload = ctx.cloud.state()
        payload["ok"] = True
        with ctx._pending_lock:
            payload["google_waiting"] = bool(ctx._pending_signin)
        self._json(payload)

    def _api_auth_signup(self) -> None:
        ctx = self._ctx()
        body = self._read_json()
        email = str(body.get("email") or "").strip()
        password = str(body.get("password") or "")
        name = str(body.get("name") or "").strip()
        if not email or not password:
            return self._error("Enter an email and a password.")
        result = ctx.cloud.sign_up(email, password, name)
        if not result.get("ok"):
            ctx.cloud.log_auth_error(
                email, f"Sign-up failed: {result.get('error')}")
            return self._error(result.get("error", "Could not create the account."), 400)
        self._json({"ok": True, "auth": ctx.cloud.state()})

    def _api_auth_signin(self) -> None:
        ctx = self._ctx()
        body = self._read_json()
        email = str(body.get("email") or "").strip()
        password = str(body.get("password") or "")
        if not email or not password:
            return self._error("Enter an email and a password.")
        result = ctx.cloud.sign_in(email, password)
        if not result.get("ok"):
            ctx.cloud.log_auth_error(
                email, f"Sign-in failed: {result.get('error')}")
            return self._error(result.get("error", "Could not sign in."), 401)
        self._json({"ok": True, "auth": ctx.cloud.state()})

    def _api_auth_google(self) -> None:
        """Open the hosted sign-in page in the real browser.

        Google refuses OAuth inside embedded webviews, so we hand the work to a
        normal web page which returns the finished session to a loopback URL.
        The UI polls /api/auth/state while this happens.
        """
        ctx = self._ctx()
        page = ctx.cloud.login_url
        if not page:
            return self._error("Google sign-in is not set up in this build.", 501)
        port = self.server.server_address[1]
        state = uuid.uuid4().hex
        with ctx._pending_lock:
            # Keep the map small: one live attempt is plenty.
            cutoff = time.time() - 900
            ctx._pending_signin = {k: v for k, v in ctx._pending_signin.items() if v > cutoff}
            ctx._pending_signin[state] = time.time()
        url = f"{page}{'&' if '?' in page else '?'}port={port}&state={state}"
        from .server import open_browser  # local import: avoids a cycle
        opened = open_browser(url)
        self._json({"ok": True, "state": state, "url": url, "opened": bool(opened)})

    def _api_auth_handoff(self) -> None:
        """The browser page posts the finished Firebase session here."""
        ctx = self._ctx()
        body = self._read_json()
        state = str(body.get("state") or "")
        with ctx._pending_lock:
            created = ctx._pending_signin.pop(state, None)
        if not created or time.time() - created > 900:
            return self._error("That sign-in link expired. Try again.", 400)
        result = ctx.cloud.adopt_tokens(body)
        if not result.get("ok"):
            ctx.cloud.log_auth_error(
                str(body.get("email") or ""),
                f"Google sign-in failed: {result.get('error')}")
            return self._error(result.get("error", "Sign-in failed."), 400)
        self._json({"ok": True, "auth": ctx.cloud.state()})

    def _api_auth_reset(self) -> None:
        ctx = self._ctx()
        body = self._read_json()
        email = str(body.get("email") or "").strip()
        if not email:
            return self._error("Enter the email address on the account.")
        result = ctx.cloud.reset_password(email)
        if not result.get("ok"):
            return self._error(result.get("error", "Could not send the email."), 400)
        self._json({"ok": True, "message": "Password reset email sent."})

    # -- API: updates ------------------------------------------------------
    def _api_update_check(self, query: dict[str, list[str]]) -> None:
        ctx = self._ctx()
        force = str(query.get("force", ["0"])[0]).lower() in ("1", "true", "yes")
        state = updater.check(ctx.cfg) if force else _cached_update_check(ctx, force=False)
        self._json({"ok": True, **state})

    def _api_update_install(self) -> None:
        ctx = self._ctx()
        body = self._read_json()
        url = (body.get("url") or "").strip()

        # A page left open for days still carries the asset URL it saw at the
        # last check, which may be an old release by now. Ask GitHub once more
        # (and refresh the cache) and prefer the newest build; if GitHub is
        # unreachable, fall back to the URL the page already has.
        state = _cached_update_check(ctx, force=True)
        fresh = ((state.get("asset") or {}).get("url") or "").strip()
        if state.get("ok") and not state.get("available"):
            return self._error(
                f"You're already on v{state.get('current')}, and the latest "
                f"published release is v{state.get('latest')}.", 409)
        if fresh:
            url = fresh
        if not url:
            cached = _cached_update_check(ctx, force=False)
            url = ((cached.get("asset") or {}).get("url") or "").strip()
        if not url:
            return self._error("No update to install.", 409)
        # Only this project's own release downloads. Without this, any website
        # could POST a URL here and have the app download and run it.
        if not updater.trusted_asset_url(url, ctx.cfg):
            return self._error("Unexpected update URL.", 400)

        def progress(event: dict[str, Any]) -> None:
            ctx.hub.emit("update_progress", event)

        result = updater.install(url, ctx.cfg, progress)
        if not result.get("ok"):
            return self._error(result.get("error", "Update failed."), 500)

        self._json({"ok": True, **result})
        # Let the reply reach the UI before the server goes away; the helper
        # script waits for the process to exit, then swaps and relaunches.
        threading.Timer(1.0, self._shutdown_soon).start()

    # -- API: one-time tool setup -----------------------------------------
    def _api_tools_install(self) -> None:
        ctx = self._ctx()
        body = self._read_json()
        force = bool(body.get("force"))

        def work() -> None:
            def progress(event: dict[str, Any]) -> None:
                ctx.hub.emit("tools_progress", event)

            result = tools.install(progress, force=force)
            if result.get("ok"):
                ctx.hub.emit("tools_progress", {
                    "stage": "done", "ready": True,
                    "message": tools.summary(),
                })
            else:
                ctx.telemetry.report_error(
                    f"Tool setup failed: {result.get('error', 'unknown')}",
                    kind="tools_failed")
                ctx.hub.emit("tools_progress", {
                    "stage": "error", "error": result.get("error", "Setup failed."),
                })

        threading.Thread(target=work, daemon=True, name="tools").start()
        self._json({"ok": True, "started": True})

    # -- API: SSE ----------------------------------------------------------
    def _api_events(self) -> None:
        ctx = self._ctx()
        types = frozenset({
            "item_progress", "item_started", "item_done", "item_failed",
            "item_stage", "item_retrying", "item_resolved",
            "run_started", "run_finished", "run_paused",
            "run_resumed", "run_stopping", "log", "playlists_changed",
            "import_progress", "compare_progress", "settings_changed",
            "queue_extended", "update_progress", "tools_progress",
        })
        sub = ctx.hub.subscribe(types)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()

            last_ping = time.time()
            while not ctx.shutdown.is_set():
                payload = ctx.hub.pump(sub, timeout=5.0)
                if payload is None:
                    if time.time() - last_ping >= 15:
                        last_ping = time.time()
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    continue
                self.wfile.write(b"data: " + payload.encode("utf-8") + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass
        finally:
            ctx.hub.unsubscribe(sub)

    # -- API: import -------------------------------------------------------
    def _api_import(self) -> None:
        if not self._require_auth():
            return
        ctx = self._ctx()
        body = self._read_json()
        source = (body.get("source") or "auto").lower()
        location = (body.get("location") or "").strip()
        mode = body.get("mode") or ctx.cfg.get("default_mode", "audio")
        if not location:
            return self._error("Paste a link or a file path first.")
        if not tool_status(ctx.cfg)["ytdlp_ok"]:
            return self._error("yt-dlp is not available. Install it, then reload.", 503)

        job_id = ctx.jobs.create("import")
        threading.Thread(
            target=_run_import,
            args=(ctx, job_id, source, location, mode, bool(body.get("autostart"))),
            daemon=True, name="import",
        ).start()
        self._json({"ok": True, "job": job_id})

    # -- API: download controls -------------------------------------------
    def _api_download_start(self) -> None:
        if not self._require_auth():
            return
        ctx = self._ctx()
        body = self._read_json()
        if not tool_status(ctx.cfg)["ffmpeg_ok"] and ctx.cfg.get("audio_format") != "original":
            ctx.hub.emit("log", {
                "level": "warning",
                "message": "ffmpeg was not found. Audio conversion and cover art need it.",
            })
        result = ctx.manager.start(
            playlist_id=body.get("playlist_id"),
            retry_failed=bool(body.get("retry_failed")),
        )
        if not result.get("ok"):
            return self._error(result.get("error", "Could not start."), 409)
        self._json({"ok": True, **result})

    # -- API: folder browse -----------------------------------------------
    def _api_browse(self, raw_path: str) -> None:
        try:
            target = _local_path(raw_path) if raw_path else Path.home().resolve()
        except (OSError, ValueError):
            return self._error("Invalid path.")
        if not target.is_dir():
            return self._error("Not a directory.")
        entries = []
        try:
            for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                if child.name.startswith("."):
                    continue
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child), "type": "dir"})
        except PermissionError:
            return self._error("Permission denied.")
        self._json({
            "ok": True,
            "path": str(target),
            "parent": str(target.parent),
            "entries": entries,
            "home": str(Path.home()),
        })

    # -- API: compare ------------------------------------------------------
    def _api_compare(self) -> None:
        if not self._require_auth():
            return
        ctx = self._ctx()
        body = self._read_json()
        folder = (body.get("folder") or "").strip()
        if not folder:
            return self._error("Choose the folder you want to check.")
        folder_path = _local_path(folder)
        if not folder_path.is_dir():
            return self._error(f"Folder not found: {folder}")

        pid = body.get("playlist_id")
        tracks: list[dict] = []
        label = body.get("label") or ""
        if pid:
            playlist = ctx.db.get_playlist(int(pid))
            if not playlist:
                return self._error("That playlist no longer exists.", 404)
            tracks = ctx.db.all_items(int(pid))
            label = label or playlist.get("name") or f"Playlist {pid}"
        else:
            tracks = body.get("tracks") or []
            label = label or "Custom list"
        if not tracks:
            return self._error("There is nothing to compare against.")

        threshold = float(body.get("threshold") or 0.72)
        recursive = bool(body.get("recursive", True))
        read_tags = bool(body.get("read_tags", True))

        job_id = ctx.jobs.create("compare")
        threading.Thread(
            target=_run_compare,
            args=(ctx, job_id, tracks, str(folder_path), threshold, recursive,
                  read_tags, label),
            daemon=True, name="compare",
        ).start()
        self._json({"ok": True, "job": job_id})

    def _api_import_missing(self) -> None:
        """Turn the 'missing' list from a comparison into a downloadable playlist."""
        if not self._require_auth():
            return
        ctx = self._ctx()
        body = self._read_json()
        items = body.get("items") or []
        name = body.get("name") or "Missing tracks"
        if not items:
            return self._error("No missing tracks were supplied.")

        prepared = []
        for it in items:
            url = (it.get("url") or "").strip()
            if not url:
                q = " - ".join(p for p in [it.get("artist"), it.get("title")] if p)
                if not q:
                    continue
                url = f"ytsearch1:{q}"
            prepared.append({
                "url": url,
                "source": "compare",
                "title": it.get("title"),
                "artist": it.get("artist"),
                "album": it.get("album"),
                "duration": it.get("duration"),
                "mode": body.get("mode") or "audio",
            })
        if not prepared:
            return self._error("None of those items had a usable URL or title.")

        pid = ctx.db.add_playlist(name, "compare", None)
        added = ctx.db.add_items(pid, prepared)
        ctx.hub.emit("playlists_changed", {})
        self._json({"ok": True, "playlist_id": pid, "added": added})

    # -- API: reveal in file manager --------------------------------------
    def _api_reveal(self) -> None:
        import subprocess
        import sys

        body = self._read_json()
        try:
            path = _local_path(body.get("path") or str(self._ctx().cfg.download_dir()))
        except (OSError, ValueError):
            return self._error("Invalid path.")
        if not path.exists():
            return self._error("Path does not exist.", 404)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)])
            elif sys.platform.startswith("win"):
                if path.is_dir():
                    subprocess.Popen(["explorer", str(path)])
                else:
                    subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
        except Exception as exc:
            return self._error(f"Could not open: {exc}")
        self._json({"ok": True})

    def _shutdown_soon(self) -> None:
        ctx = self._ctx()
        time.sleep(0.3)
        ctx.shutdown.set()
        try:
            ctx.manager.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------

def build_server(ctx: AppContext, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    global CTX
    CTX = ctx
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server