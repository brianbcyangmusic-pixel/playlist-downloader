"""Usage logging: what each signed-in user downloaded, and what went wrong.

The app already broadcasts lifecycle events through :mod:`app.events`. This
module listens to them and writes one Firestore document per *playlist
download* (so the admin dashboard can show "who, which playlist, which link"),
plus a document for every failure worth telling the developer about.

Everything happens on a background thread behind a bounded queue: a slow or
unreachable database can never stall a download, and if the queue is full the
event is dropped rather than blocking the worker.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Any

from . import __version__
from .cloud import Cloud

MAX_QUEUE = 200

_SCREEN_WORTHY = {"run_started", "run_finished", "item_failed"}


def os_name() -> str:
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("win"):
        return "Windows"
    return "Linux"


class Telemetry:
    """Turns app events into dashboard rows. Silent when accounts are off."""

    def __init__(self, cloud: Cloud, db: Any) -> None:
        self.cloud = cloud
        self.db = db
        self._queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(MAX_QUEUE)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # Playout of the run in progress: doc id -> playlist snapshot.
        self._active: dict[str, dict[str, Any]] = {}
        self._run_started = 0.0
        self._run_failed = 0
        self._run_base: dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------
    def start(self, hub: Any) -> None:
        if not self.cloud.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        hub.on("telemetry", self._on_event)
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="telemetry")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(("__stop__", {}))
        except queue.Full:
            pass

    # -- event intake (must stay cheap: called on worker threads) ----------
    def _on_event(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type not in _SCREEN_WORTHY:
            return
        try:
            self._queue.put_nowait((event_type, dict(data)))
        except queue.Full:
            pass

    # -- direct reports (called from the API layer) ------------------------
    def report_download(self, *, playlist: dict[str, Any], status: str = "started",
                        extra: dict[str, Any] | None = None) -> str | None:
        """Write the row for a playlist the user imported or started."""
        if not self.cloud.enabled:
            return None
        user = self.cloud.state().get("user") or {}
        doc_id = f"pl{playlist.get('id')}_{int(time.time() * 1000)}"
        fields = {
            "uid": user.get("uid", ""),
            "user_email": user.get("email", ""),
            "kind": "playlist",
            "playlist": playlist.get("name") or "",
            "platform": playlist.get("source") or "",
            "link": playlist.get("source_url") or "",
            "tracks_total": int(playlist.get("total") or 0),
            "tracks_done": int(playlist.get("done") or 0),
            "tracks_failed": int(playlist.get("failed") or 0),
            "tracks_skipped": int(playlist.get("skipped") or 0),
            "status": status,
            "os": os_name(),
            "app_version": __version__,
            "created_at": _now_iso(),
            "started_at": _now_iso(),
        }
        if extra:
            fields.update(extra)
        sample = playlist.get("sample_tracks") or []
        if sample:
            fields["sample_tracks"] = sample
        result = self.cloud.log_download(doc_id=doc_id, **fields)
        if not result.get("ok"):
            return None
        with self._lock:
            self._active[doc_id] = dict(fields)
        return doc_id

    def report_error(self, message: str, kind: str = "app_error",
                     **fields: Any) -> None:
        if not self.cloud.enabled:
            return
        user = self.cloud.state().get("user") or {}
        fields.setdefault("uid", user.get("uid", ""))
        fields.setdefault("user_email", user.get("email", ""))
        fields.setdefault("os", os_name())
        try:
            self._queue.put_nowait(("error", {"message": message, "kind": kind,
                                              "fields": fields}))
        except queue.Full:
            pass

    # -- the worker --------------------------------------------------------
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                kind, data = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if kind == "__stop__":
                return
            try:
                if kind == "error":
                    self._write_error(data)
                elif kind == "run_started":
                    self._begin_run()
                elif kind == "run_finished":
                    self._finish_run(data.get("stats") or {})
                elif kind == "item_failed":
                    self._run_failed += 1
            except Exception:
                # Logging must never take the app down with it.
                pass

    def _begin_run(self) -> None:
        if not self.cloud.enabled:
            return
        self._run_started = time.time()
        self._run_failed = 0
        try:
            self._run_base = dict(self.db.stats() or {})
        except Exception:
            self._run_base = {}
        # If a previous run never reported a finish (the app was quit or
        # crashed mid-download), close those rows honestly before starting.
        with self._lock:
            leftovers = dict(self._active)
            self._active.clear()
        for doc_id in leftovers:
            self.cloud.write("downloads", {"status": "interrupted",
                                           "finished_at": _now_iso()},
                             doc_id=doc_id, update=True)
        # Anything still queued belongs to this run; give each playlist a row.
        for row in self._pending_playlists():
            self.report_download(playlist=row, status="started")

    def _finish_run(self, stats: dict[str, Any] | None = None) -> None:
        with self._lock:
            docs = dict(self._active)
            self._active.clear()
        failed = max(0, self._run_failed)
        rows = {int(r["id"]): r for r in self._safe_playlists()}
        for doc_id, snapshot in docs.items():
            pid = _playlist_id(snapshot, doc_id)
            row = rows.get(pid or -1, {})
            done = int(row.get("done") or 0)
            bad = int(row.get("failed") or 0)
            if bad and done:
                status = "partial"
            elif bad:
                status = "failed"
            else:
                status = "done"
            self.cloud.write("downloads", {
                "status": status,
                "tracks_total": int(row.get("total") or snapshot.get("tracks_total") or 0),
                "tracks_done": done,
                "tracks_failed": bad,
                "tracks_skipped": int(row.get("skipped") or 0),
                "finished_at": _now_iso(),
                "duration_s": int(time.time() - self._run_started) if self._run_started else 0,
            }, doc_id=doc_id, update=True)

        if failed:
            names = ", ".join(s.get("playlist") or "?" for s in docs.values()) or "playlist"
            self.report_error(
                f"{failed} track(s) failed while downloading {names}.",
                kind="download_failed",
                link=next((s.get("link") for s in docs.values() if s.get("link")), ""),
                tracks_failed=failed,
            )

    def _write_error(self, data: dict[str, Any]) -> None:
        self.cloud.log_error(data.get("message", ""), kind=data.get("kind", "app_error"),
                             **data.get("fields", {}))

    # -- helpers -----------------------------------------------------------
    def _safe_playlists(self) -> list[dict[str, Any]]:
        try:
            return list(self.db.list_playlists())
        except Exception:
            return []

    def _pending_playlists(self) -> list[dict[str, Any]]:
        rows = []
        for row in self._safe_playlists():
            if int(row.get("pending") or 0) > 0:
                row = dict(row)
                row["sample_tracks"] = self._sample_tracks(int(row["id"]))
                rows.append(row)
        return rows

    def _sample_tracks(self, playlist_id: int) -> list[str]:
        """A few titles for context, so a row is identifiable at a glance."""
        try:
            items = self.db.get_items(playlist_id, limit=12)
        except Exception:
            return []
        titles = []
        for item in items:
            title = (item.get("title") or "").strip()
            artist = (item.get("artist") or "").strip()
            if title:
                titles.append(f"{artist} – {title}" if artist else title)
        return titles[:12]


def _playlist_id(snapshot: dict[str, Any], doc_id: str) -> int | None:
    """Recover the playlist id from the document id we generated (``pl12_...``)."""
    try:
        return int(doc_id[2:].split("_", 1)[0])
    except (ValueError, IndexError):
        return None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
