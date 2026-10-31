"""The download engine.

Design notes for large playlists (1500+ tracks)
-----------------------------------------------
* A small pool of worker threads (network-bound, so 3 is usually optimal).
* Every state change is written to SQLite, so a crash, a closed browser or a
  reboot only ever costs the in-flight items.
* `download_archive` means re-running a playlist skips finished items entirely.
* yt-dlp is used as a *library*, giving us progress hooks, precise error text
  and post-processing without parsing shell output.
* Tracks without a direct URL (Spotify / text files) are stored as
  `ytsearch1:Artist - Title`, so the lookup happens inside the worker pool and
  runs in parallel instead of in one slow serial pass.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import yt_dlp

from . import paths, tagging, tools
from .config import Config, network_opts
from .db import QueueDB
from .events import EventHub
from .matcher import normalize, _ratio

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class Cancelled(Exception):
    """Raised from inside a yt-dlp progress hook to abort a download."""


def _best_thumbnail(info: dict[str, Any]) -> str | None:
    """Pick the largest square-ish thumbnail yt-dlp reported."""
    thumbs = info.get("thumbnails") or []
    best, best_area = None, -1
    for t in thumbs:
        url = t.get("url")
        if not url:
            continue
        area = (t.get("width") or 0) * (t.get("height") or 0)
        if area > best_area:
            best, best_area = url, area
    return best or info.get("thumbnail")


def sanitize_filename(name: str, max_len: int = 150) -> str:
    """Make a string safe on Windows, macOS and Linux simultaneously."""
    name = _ILLEGAL.sub("_", name).strip()
    name = re.sub(r"\s+", " ", name)
    name = name.rstrip(" .")
    if not name:
        name = "Untitled"
    if name.upper().split(".")[0] in _RESERVED:
        name = "_" + name
    if len(name) > max_len:
        root, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 5:
            name = root[: max_len - len(ext) - 1] + "." + ext
        else:
            name = name[:max_len]
    return name


def _video_id(url: str) -> str | None:
    """Extract a YouTube video id from a watch/short URL."""
    if not url:
        return None
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def _is_transient(message: str) -> bool:
    """Heuristic: is this error worth retrying?"""
    text = message.lower()
    markers = (
        "403", "429", "forbidden", "too many requests", "rate limit",
        "timed out", "timeout", "connection reset", "connection aborted",
        "temporarily unavailable", "503", "502", "500", "server error",
        "unable to download video data", "read timed out",
        "incomplete read", "ssl", "network is unreachable",
        "remote end closed connection", "giving up after",
    )
    return any(m in text for m in markers)


class _YdlLogger:
    """Route yt-dlp's own messages into the UI event stream."""

    def __init__(self, hub: EventHub, item_id: int | None, quiet: bool = True):
        self.hub = hub
        self.item_id = item_id
        self.quiet = quiet

    def debug(self, msg):
        if msg.startswith("[debug] "):
            return
        if not self.quiet:
            self.hub.emit("log", {"level": "debug", "item_id": self.item_id, "message": str(msg)[:500]})

    def info(self, msg):
        if not self.quiet:
            self.hub.emit("log", {"level": "info", "item_id": self.item_id, "message": str(msg)[:500]})

    def warning(self, msg):
        self.hub.emit("log", {"level": "warning", "item_id": self.item_id, "message": str(msg)[:500]})

    def error(self, msg):
        self.hub.emit("log", {"level": "error", "item_id": self.item_id, "message": str(msg)[:500]})


class DownloadManager:
    """Owns the worker pool and the lifecycle of a download run."""

    def __init__(self, cfg: Config, db: QueueDB, hub: EventHub) -> None:
        self.cfg = cfg
        self.db = db
        self.hub = hub
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._running = False
        self._active: dict[int, dict[str, Any]] = {}   # item_id -> live progress
        self._last_emit: dict[int, float] = {}
        self._queue: list[int] = []
        self._queue_lock = threading.Lock()
        self._auto_retry_passes = 0
        self._archive_cache: set[str] | None = None
        self._archive_mtime: float = 0.0
        self._playlist_cache: dict[int, dict[str, Any]] = {}   # playlist_id -> {name, total}

    # -- public state ------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "paused": self._pause.is_set(),
                "active": list(self._active.values()),
                "queued": len(self._queue),
                "stats": self.db.stats(),
            }

    # -- lifecycle ---------------------------------------------------------
    def start(self, playlist_id: int | None = None, retry_failed: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._running:
                return {"ok": False, "error": "Already running."}
            if retry_failed:
                self.db.retry_failed(playlist_id)
            items = [
                it for it in self.db.get_items(playlist_id, status="pending", limit=10**6)
            ]
            if not items:
                return {"ok": False, "error": "Nothing pending to download."}

            # Never start an audio run that would silently leave WebM files on
            # disk; conversion needs FFmpeg (and ffprobe) present up front.
            needs_ffmpeg = (self.cfg.get("audio_format") or "mp3") != "original"
            if needs_ffmpeg and any(self._is_audio(it) for it in items):
                if not tools.status()["ffmpeg_ok"]:
                    return {
                        "ok": False,
                        "error": "FFmpeg is missing, so audio cannot be converted. "
                                 "Finish the one-time setup, then press Download again.",
                    }

            self._stop.clear()
            self._pause.clear()
            self._running = True
            self._auto_retry_passes = 0
            self._playlist_cache.clear()
            with self._queue_lock:
                self._queue = [it["id"] for it in items]

            self._clean_thumbnails()

            workers = max(1, min(int(self.cfg.get("concurrent_downloads") or 3), 12))
            self._threads = [
                threading.Thread(target=self._worker, name=f"dl-{i}", daemon=True)
                for i in range(workers)
            ]
            self.hub.emit("run_started", {"queued": len(items), "workers": workers})
            for t in self._threads:
                t.start()
            return {"ok": True, "queued": len(items), "workers": workers}

    def stop(self, wait: bool = False, timeout: float = 30) -> dict[str, Any]:
        self._stop.set()
        self._pause.clear()
        if wait:
            for t in list(self._threads):
                t.join(timeout=timeout)
        self.hub.emit("run_stopping", {})
        return {"ok": True}

    def pause(self) -> dict[str, Any]:
        self._pause.set()
        self.hub.emit("run_paused", {})
        return {"ok": True, "paused": True}

    def resume(self) -> dict[str, Any]:
        self._pause.clear()
        self.hub.emit("run_resumed", {})
        return {"ok": True, "paused": False}

    def _clean_thumbnails(self) -> None:
        """Clear scratch thumbnail files left over from earlier runs."""
        try:
            for f in paths.thumb_dir().glob("*"):
                if f.is_file():
                    f.unlink(missing_ok=True)
        except OSError:
            pass

    def add_to_queue(self, item_ids: list[int]) -> int:
        """Append items to a run that is already going."""
        added = 0
        with self._queue_lock:
            existing = set(self._queue)
            for i in item_ids:
                if i not in existing:
                    self._queue.append(i)
                    added += 1
                    self.db.mark_pending(i)
        if added and self._running:
            self.hub.emit("queue_extended", {"added": added})
        return added

    # -- worker ------------------------------------------------------------
    def _next(self) -> int | None:
        with self._queue_lock:
            return self._queue.pop(0) if self._queue else None

    def _worker(self) -> None:
        while not self._stop.is_set():
            while self._pause.is_set() and not self._stop.is_set():
                time.sleep(0.3)
            if self._stop.is_set():
                break
            item_id = self._next()
            if item_id is None:
                # Queue drained — the last worker out decides whether to make a
                # cleanup pass over transient failures before ending the run.
                if self._try_auto_retry():
                    continue
                break
            item = self.db.get_item(item_id)
            if not item:
                continue
            self._attempt_item(item)

        with self._lock:
            # Only the last worker to finish flips the run off.
            if all(not t.is_alive() or t is threading.current_thread() for t in self._threads):
                if self._running and not self._queue_has_items():
                    self._running = False
                    self.hub.emit("run_finished", {"stats": self.db.stats()})

    def _attempt_item(self, item: dict[str, Any]) -> None:
        """Download one item, retrying transient network errors with backoff.

        YouTube throttles bursts of parallel requests, so a 403/429 is usually
        temporary rather than fatal. Retrying in place is what keeps a
        1500-track run from collecting hundreds of spurious failures.
        """
        item_id = item["id"]
        max_attempts = int(self.cfg.get("item_retries") or 4)
        base_delay = float(self.cfg.get("retry_delay_seconds") or 8)

        for attempt in range(1, max_attempts + 1):
            try:
                self._download_one(item)
                return
            except Cancelled:
                self.db.update_item(item_id, status="pending", progress=0,
                                    speed=None, eta=None)
                return
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                transient = _is_transient(message)
                if transient and attempt < max_attempts and not self._stop.is_set():
                    delay = min(base_delay * (2 ** (attempt - 1)), 120)
                    self.hub.emit("item_retrying", {
                        "id": item_id, "title": item.get("title"),
                        "attempt": attempt, "of": max_attempts,
                        "delay": round(delay), "error": message[:200],
                    })
                    self.hub.emit("log", {
                        "level": "warning", "item_id": item_id,
                        "message": f"Retry {attempt}/{max_attempts - 1} for "
                                   f"“{item.get('title')}” in {round(delay)}s ({message[:90]})",
                    })
                    # Sleep, but stay responsive to Stop.
                    slept = 0.0
                    while slept < delay and not self._stop.is_set():
                        time.sleep(0.5)
                        slept += 0.5
                    continue
                self.db.mark_failed(item_id, message,
                                    retries=int(item.get("retries") or 0) + 1)
                self.hub.emit("item_failed", {
                    "id": item_id, "title": item.get("title"),
                    "error": message[:400],
                })
                return

    def _try_auto_retry(self) -> bool:
        """One extra sweep over failures after a cooldown.

        Long runs almost always end with a handful of transient failures; this
        clears them without the user having to press Retry.
        """
        if not self.cfg.get("auto_retry_failed", True):
            return False
        with self._lock:
            if self._auto_retry_passes >= int(self.cfg.get("auto_retry_passes") or 1):
                return False
        stats = self.db.stats()
        if stats["failed"] == 0 or stats["pending"] > 0:
            return False

        cooldown = float(self.cfg.get("auto_retry_cooldown") or 60)
        with self._lock:
            self._auto_retry_passes += 1
        self.hub.emit("log", {
            "level": "info",
            "message": f"{stats['failed']} item(s) failed. Waiting {int(cooldown)}s, "
                       "then retrying them once automatically…",
        })
        slept = 0.0
        while slept < cooldown and not self._stop.is_set():
            time.sleep(1.0)
            slept += 1.0
        if self._stop.is_set():
            return False

        requeued = self.db.retry_failed()
        if not requeued:
            return False
        pending = self.db.get_items(status="pending", limit=10**6)
        with self._queue_lock:
            existing = set(self._queue)
            for it in pending:
                if it["id"] not in existing:
                    self._queue.append(it["id"])
        self._running = True
        self.hub.emit("run_started", {"queued": requeued, "workers": 1, "auto_retry": True})
        return True

    def _queue_has_items(self) -> bool:
        with self._queue_lock:
            return bool(self._queue)

    # -- archives & existing files -----------------------------------------
    def _in_archive(self, video_id: str) -> bool:
        """Check yt-dlp's download archive ourselves.

        yt-dlp silently returns None for archived videos, which is
        indistinguishable from a deleted video unless we look it up.
        """
        path = paths.archive_file()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        with self._lock:
            if self._archive_cache is None or mtime != self._archive_mtime:
                ids: set[str] = set()
                try:
                    for line in path.read_text("utf-8", errors="ignore").splitlines():
                        parts = line.split()
                        if len(parts) >= 2:
                            ids.add(parts[-1])
                except OSError:
                    return False
                self._archive_cache = ids
                self._archive_mtime = mtime
            return video_id in self._archive_cache

    def _forget_archive(self, video_id: str) -> None:
        """Drop a stale archive entry so the item can be downloaded again."""
        path = paths.archive_file()
        try:
            if not path.exists():
                return
            with self._lock:
                lines = path.read_text("utf-8", errors="ignore").splitlines()
                kept = [ln for ln in lines if ln.split()[-1:] != [video_id]]
                path.write_text("\n".join(kept) + ("\n" if kept else ""), "utf-8")
                self._archive_cache = None      # force a reload
        except OSError:
            pass

    def _wanted_extensions(self, item: dict[str, Any]) -> tuple[str, ...]:
        """Which on-disk extensions count as a finished item.

        When the requested audio format is MP3 (or M4A/FLAC/…), a leftover
        ``.webm`` from a failed conversion is *not* a finished item — otherwise
        the skip-existing check would hide the bug forever.
        """
        if self._is_audio(item) and (self.cfg.get("audio_format") or "mp3") != "original":
            fmt = (self.cfg.get("audio_format") or "mp3").lower()
            wanted = {"m4a": ".m4a", "opus": ".opus", "flac": ".flac", "wav": ".wav"}.get(
                fmt, ".mp3"
            )
            return (wanted,)
        return (".mp3", ".m4a", ".opus", ".flac", ".wav", ".ogg",
                ".mp4", ".mkv", ".webm")

    def _find_existing_file(self, item: dict[str, Any]) -> str | None:
        """Look for a file already downloaded under the expected name."""
        roots = [self._item_folder_path(item)]
        home = self.cfg.download_dir()
        if roots[0] != home:
            roots.append(home)          # older runs may have written it flat
        artist = item.get("artist") or ""
        title = item.get("title") or ""
        stems: list[str] = []
        if artist and title:
            stems.append(f"{artist} - {title}")
        if title:
            stems.append(title)
        extensions = self._wanted_extensions(item)
        for root in roots:
            for stem in stems:
                safe = sanitize_filename(stem)
                for ext in extensions:
                    candidate = root / f"{safe}{ext}"
                    if candidate.exists():
                        return str(candidate)
        return None

    def _recover_archive_skip(self, item: dict[str, Any], info: dict[str, Any],
                              opts: dict[str, Any], url: str) -> tuple[bool, str | None]:
        """Handle a yt-dlp archive hit that produced no output file.

        The pre-check in ``_download_one`` only recognises YouTube urls, but
        yt-dlp skips *any* id already recorded in its archive (a plain/generic
        url, for example) and returns the info dict with nothing written to
        disk. That is a skip rather than a failure, so mark it skipped when the
        file is still on disk; when the file is gone the archive entry is stale,
        so drop it and download the track again.

        Returns ``(handled, filepath)``: ``handled`` means the caller should stop
        (the item is finished), otherwise ``filepath`` may hold a re-downloaded
        file.
        """
        info_id = str(info.get("id") or "")
        if not info_id or not self.cfg.get("use_archive") or not self._in_archive(info_id):
            return False, None

        found = self._find_existing_file(item)
        if found:
            self.db.update_item(item["id"], status="skipped", progress=100,
                                filepath=found, error=None)
            self.hub.emit("item_done", {
                "id": item["id"], "title": item.get("title"), "filepath": found,
                "skipped": True, "reason": "already downloaded earlier",
            })
            return True, None

        # Stale entry: the file is gone, so drop it and fetch the track again.
        self._forget_archive(info_id)
        self.hub.emit("log", {
            "level": "info", "item_id": item["id"],
            "message": f"“{item.get('title')}” was in the archive but the file "
                       "is missing — downloading it again.",
        })
        retry_opts = self._build_opts(item)
        with yt_dlp.YoutubeDL(retry_opts) as ydl:
            again = ydl.extract_info(url, download=True)
        if not again:
            return False, None
        if "entries" in again:
            entries = [e for e in (again.get("entries") or []) if e]
            if not entries:
                return False, None
            again = entries[0]
        return False, self._resolve_filepath(again, retry_opts)

    # -- search resolution -------------------------------------------------
    def _resolve_search(self, url: str, item: dict[str, Any]) -> str | None:
        """Turn a `ytsearch` query into a direct video URL, choosing the entry
        that best matches the intended track.

        Scoring blends title similarity with duration agreement. A full-album
        or 1-hour loop upload is heavily penalised, which is the single biggest
        quality win when resolving a large playlist made of song names.
        """
        query = url.split(":", 1)[1] if ":" in url else url
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "ignoreerrors": True,
            "noplaylist": True,
            "skip_download": True,
        }
        opts.update(network_opts(self.cfg))

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch5:{query}", download=False)
        except Exception as exc:
            self.hub.emit("log", {"level": "warning", "item_id": item["id"],
                                  "message": f"Search failed: {exc}"})
            return None

        entries = [e for e in ((info or {}).get("entries") or []) if e]
        if not entries:
            return None

        best_id, best_score = None, -1.0
        for entry in entries:
            if not entry.get("id"):
                continue
            score = self._candidate_score(entry, item)
            if score > best_score:
                best_score, best_id = score, entry["id"]

        if best_id is None:
            return None
        return f"https://www.youtube.com/watch?v={best_id}"

    def _candidate_score(self, entry: dict[str, Any], item: dict[str, Any]) -> float:
        want_title = normalize(item.get("title") or "")
        want_artist = normalize(item.get("artist") or "")
        got_title = normalize(entry.get("title") or "")

        title_score = _ratio(want_title, got_title) if want_title else 0.5
        # Token containment helps when the uploader adds channel junk.
        want_tokens = set(want_title.split())
        got_tokens = set(got_title.split())
        if want_tokens and got_tokens:
            cover = len(want_tokens & got_tokens) / min(len(want_tokens), len(got_tokens))
            title_score = max(title_score, cover)

        score = title_score * 1.0

        if want_artist:
            channel = normalize(
                entry.get("channel") or entry.get("uploader") or entry.get("artist") or ""
            )
            if channel and want_artist:
                score += 0.25 * _ratio(want_artist, channel)

        # Duration is the tie-breaker that kills full-album uploads.
        expected = item.get("duration")
        got = entry.get("duration")
        if expected and got:
            delta = abs(float(expected) - float(got))
            if delta <= 3:
                score += 0.35
            elif delta <= 10:
                score += 0.15
            elif delta <= max(20, float(expected) * 0.15):
                score += 0.0
            else:
                score -= 0.9            # wrong song, or a full album
        elif got:
            # No expected duration: reject the obvious album/loop uploads.
            if got > 1200:
                score -= 1.2            # 20+ minutes is not a single song
            elif got > 600:
                score -= 0.5
            elif got <= 480:
                score += 0.1
        return score

    # -- single item -------------------------------------------------------
    def _download_one(self, item: dict[str, Any]) -> None:
        item_id = item["id"]
        url = item.get("url") or ""
        if not url:
            self.db.mark_failed(item_id, "No URL for this item.", int(item.get("retries") or 0) + 1)
            return

        # A search query means "find the right track first". Blindly taking the
        # first hit is how you end up with a 35-minute full-album upload, so we
        # score the candidates against the expected title/artist/duration.
        if url.startswith(("ytsearch", "ytsearch1:", "ytsearch5:")):
            resolved = self._resolve_search(url, item)
            if not resolved:
                raise RuntimeError(
                    f"No usable YouTube match for “{item.get('title') or url}”."
                )
            url = resolved
            # Remember the real video so retries and the UI show what was chosen.
            self.db.update_item(item_id, url=resolved)
            self.hub.emit("item_resolved", {
                "id": item_id, "url": resolved,
                "title": item.get("title"), "artist": item.get("artist"),
            })

        # Already on disk from a previous run? (by recorded path, or by name)
        if self.cfg.get("skip_existing_files"):
            wanted = self._wanted_extensions(item)
            existing = item.get("filepath")
            if existing and Path(existing).exists() \
                    and Path(existing).suffix.lower() not in wanted:
                # A .webm left by the 1.4.4 bug is not "already downloaded" when
                # the user asked for MP3: let yt-dlp convert or fetch it again.
                existing = None
            if not existing or not Path(existing).exists():
                existing = self._find_existing_file(item)
            if existing and Path(existing).exists():
                self.db.update_item(item_id, status="skipped", progress=100,
                                    filepath=existing, error=None)
                self.hub.emit("item_done", {
                    "id": item_id, "title": item.get("title"),
                    "filepath": existing, "skipped": True, "reason": "file already on disk",
                })
                return

        # Already in yt-dlp's archive? yt-dlp returns None for these, which is
        # not a real failure — it means we already have the track. But only
        # trust the archive if the file is actually still on disk; otherwise the
        # user deleted it and we should fetch it again.
        video_id = _video_id(url)
        if video_id and self.cfg.get("use_archive") and self._in_archive(video_id):
            found = self._find_existing_file(item)
            if found:
                self.db.update_item(item_id, status="skipped", progress=100,
                                    filepath=found, error=None)
                self.hub.emit("item_done", {
                    "id": item_id, "title": item.get("title"), "filepath": found,
                    "skipped": True, "reason": "already downloaded earlier",
                })
                return
            # File is gone — the archive entry is stale, so drop it and refetch.
            self._forget_archive(video_id)
            self.hub.emit("log", {
                "level": "info", "item_id": item_id,
                "message": f"“{item.get('title')}” was in the archive but the file "
                           "is missing — downloading it again.",
            })

        self.db.update_item(item_id, status="downloading", progress=0, error=None)
        self.hub.emit("item_started", {
            "id": item_id, "title": item.get("title"),
            "artist": item.get("artist"), "url": url,
        })

        opts = self._build_opts(item)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        if info is None:
            # A race can land here if the archive was written by another worker.
            if video_id and self._in_archive(video_id):
                found = self._find_existing_file(item)
                self.db.update_item(item_id, status="skipped", progress=100,
                                    filepath=found, error=None)
                self.hub.emit("item_done", {
                    "id": item_id, "title": item.get("title"), "filepath": found,
                    "skipped": True, "reason": "already downloaded earlier",
                })
                return
            raise RuntimeError("yt-dlp returned no information (video unavailable or removed).")

        # Entries can be a playlist container if a whole channel slipped through.
        if "entries" in info:
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise RuntimeError("No downloadable entries found.")
            info = entries[0]

        if self.cfg.get("dry_run"):
            self.db.mark_done(item_id, "[dry run]")
            self.hub.emit("item_done", {
                "id": item_id, "title": item.get("title"),
                "filepath": "[dry run]", "file": "[dry run]", "dry_run": True,
            })
            return

        filepath = self._resolve_filepath(info, opts)
        if filepath is None:
            handled, filepath = self._recover_archive_skip(item, info, opts, url)
            if handled:
                return
        if filepath is None:
            raise RuntimeError("Download finished but no output file was found.")

        # Rename + tag with the playlist's own metadata when we have it.
        final = self._finalize(item, info, filepath)
        self.db.mark_done(item_id, final)
        self.hub.emit("item_done", {
            "id": item_id, "title": item.get("title"),
            "filepath": final, "file": Path(final).name,
        })

    # -- yt-dlp options ----------------------------------------------------
    def _playlist_folder(self, item: dict[str, Any]) -> str:
        """Sub-folder name for an item's playlist, or ``""`` to write flat.

        A playlist with a single track is really a single file, so it lands
        straight in the destination under its own name. Anything longer gets a
        folder named after the playlist, so a big download stays tidy.
        """
        if not self.cfg.get("make_playlist_subfolder", True):
            return ""
        pid = item.get("playlist_id")
        if pid is None:
            return ""
        with self._lock:
            cached = self._playlist_cache.get(pid)
        if cached is None:
            pl = self.db.get_playlist(pid) or {}
            cached = {"name": pl.get("name") or "", "total": self.db.count_items(pid)}
            with self._lock:
                self._playlist_cache[pid] = cached
        if cached["total"] <= 1 or not cached["name"]:
            return ""
        return sanitize_filename(cached["name"])

    def _item_folder_path(self, item: dict[str, Any]) -> Path:
        """Destination directory that holds this item (playlist folder applied)."""
        home = self.cfg.download_dir()
        folder = self._playlist_folder(item)
        return home / folder if folder else home

    def _build_opts(self, item: dict[str, Any]) -> dict[str, Any]:
        cfg = self.cfg
        item_id = item["id"]
        is_audio = self._is_audio(item)
        home = str(cfg.download_dir())

        outtmpl = cfg.get("audio_filename") if is_audio else cfg.get("video_filename")
        outtmpl = self._expand_template(outtmpl, is_audio)
        # Give a multi-track playlist its own folder (unless the user already
        # pointed the template at an absolute path).
        folder = self._playlist_folder(item)
        if folder and not Path(outtmpl).is_absolute():
            outtmpl = f"{folder}/{outtmpl}"

        opts: dict[str, Any] = {
            "outtmpl": {
                "default": outtmpl,
                # Keep thumbnail scratch files out of the music folder — a
                # failed download used to leave a stray .webp behind.
                "thumbnail": str(paths.thumb_dir() / "%(id)s.%(ext)s"),
            },
            "paths": {"home": home},
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "progress_hooks": [self._progress_hook(item_id)],
            "postprocessor_hooks": [self._pp_hook(item_id)],
            "logger": _YdlLogger(self.hub, item_id, quiet=bool(cfg.get("quiet_logs"))),
            "ignoreerrors": False,
            "noplaylist": True,
            "simulate": bool(cfg.get("dry_run")),
            "retries": int(cfg.get("retries") or 10),
            "fragment_retries": int(cfg.get("retries") or 10),
            "extractor_retries": 3,
            "socket_timeout": int(cfg.get("socket_timeout") or 30),
            "continuedl": True,
            "overwrites": False,
            "concurrent_fragment_downloads": max(1, int(cfg.get("concurrent_fragments") or 4)),
            "cachedir": str(paths.cache_dir()),
            "windowsfilenames": True,
            "trim_file_name": 120,
            "restrictfilenames": False,
            "retry_sleep_functions": {"http": lambda n: min(4 * n, 30)},
        }

        # Proxy, cookies, ffmpeg location and the JS challenge runtime.
        opts.update(network_opts(cfg))

        if cfg.get("use_archive"):
            opts["download_archive"] = str(paths.archive_file())

        if cfg.get("rate_limit_kbps"):
            opts["ratelimit"] = int(cfg.get("rate_limit_kbps")) * 1024

        if cfg.get("sleep_between_requests"):
            opts["sleep_interval_requests"] = float(cfg.get("sleep_between_requests"))

        if is_audio:
            opts.update(self._audio_opts())
        else:
            opts.update(self._video_opts())
        return opts

    def _is_audio(self, item: dict[str, Any]) -> bool:
        """Music playlists default to audio; explicit video links get video."""
        kind = (item.get("source") or "").lower()
        if kind in ("spotify", "file"):
            return True
        if item.get("mode") in ("audio", "video"):
            return item["mode"] == "audio"
        return self.cfg.get("default_mode", "audio") == "audio"

    def _audio_opts(self) -> dict[str, Any]:
        cfg = self.cfg
        fmt = cfg.get("audio_format") or "mp3"
        quality = str(cfg.get("audio_quality") or "320")

        opts: dict[str, Any] = {"format": "bestaudio/best"}
        post: list[dict[str, Any]] = []

        if fmt != "original":
            codec = {"m4a": "m4a", "opus": "opus", "flac": "flac", "wav": "wav"}.get(fmt, "mp3")
            if quality == "best":
                preferred = "0"
            elif codec in ("flac", "wav"):
                preferred = "0"
            else:
                preferred = quality
            post.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec,
                "preferredquality": preferred,
            })

        post.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": False})

        # Cover art is embedded by mutagen after the download, not by yt-dlp.
        # That avoids leaving stray thumbnail files behind when a download
        # fails, and lets Spotify album art win over a YouTube still frame.
        opts["postprocessors"] = post
        return opts

    def _video_opts(self) -> dict[str, Any]:
        cfg = self.cfg
        fmt = cfg.get("video_format") or "mp4"
        height = cfg.get("video_quality") or "1080"
        if height == "best" or not str(height).isdigit():
            selector = "bestvideo+bestaudio/best"
        else:
            h = int(height)
            selector = (
                f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
            )
        opts: dict[str, Any] = {
            "format": selector,
            "merge_output_format": fmt,
            "postprocessors": [{"key": "FFmpegMetadata", "add_metadata": True}],
        }
        if cfg.get("embed_cover_art"):
            opts["writethumbnail"] = True
            opts["postprocessors"].append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
        return opts

    def _expand_template(self, template: str, is_audio: bool) -> str:
        """Add safe fallbacks so a missing field never produces 'NA' files."""
        if is_audio:
            template = template.replace("%(artist)s", "%(artist,uploader,channel|Unknown Artist)s")
            template = template.replace("%(title)s", "%(track,title|Untitled)s")
        else:
            template = template.replace("%(title)s", "%(title|Untitled)s")
        return template

    # -- hooks -------------------------------------------------------------
    def _progress_hook(self, item_id: int):
        def hook(d: dict[str, Any]) -> None:
            if self._stop.is_set():
                raise Cancelled()
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                pct = (downloaded / total * 100) if total else 0.0
                speed = d.get("speed")
                eta = d.get("eta")
                info = d.get("info_dict") or {}
                title = info.get("title")
                with self._lock:
                    self._active[item_id] = {
                        "id": item_id,
                        "title": title,
                        "progress": round(pct, 2),
                        "speed": speed,
                        "eta": eta,
                        "downloaded": downloaded,
                        "total": total,
                        "stage": "downloading",
                    }
                now = time.time()
                if now - self._last_emit.get(item_id, 0) >= 0.4:
                    self._last_emit[item_id] = now
                    self.db.update_item(item_id, progress=round(pct, 2), speed=speed, eta=eta)
                    self.hub.emit("item_progress", {
                        "id": item_id, "progress": round(pct, 2), "speed": speed,
                        "eta": eta, "downloaded": downloaded, "total": total,
                        "title": title,
                    })
            elif status == "finished":
                self.hub.emit("item_stage", {"id": item_id, "stage": "processing"})
                with self._lock:
                    if item_id in self._active:
                        self._active[item_id]["stage"] = "processing"
                        self._active[item_id]["progress"] = 100
                self.db.update_item(item_id, progress=100, speed=None, eta=None)
        return hook

    def _pp_hook(self, item_id: int):
        def hook(d: dict[str, Any]) -> None:
            if self._stop.is_set():
                raise Cancelled()
            name = (d.get("postprocessor") or "").split(".")[-1]
            if d.get("status") == "started":
                self.hub.emit("item_stage", {"id": item_id, "stage": name})
        return hook

    # -- output handling ---------------------------------------------------
    def _resolve_filepath(self, info: dict[str, Any], opts: dict[str, Any]) -> str | None:
        requested = info.get("requested_downloads") or []
        for entry in requested:
            fp = entry.get("filepath")
            if fp and Path(fp).exists():
                return fp
        fp = info.get("filepath") or info.get("_filename")
        if fp and Path(fp).exists():
            return fp
        # Post-processing (audio extraction) changes the extension.
        if fp:
            stem = Path(fp).with_suffix("")
            for ext in (".mp3", ".m4a", ".opus", ".flac", ".wav",
                        ".mp4", ".mkv", ".webm", ".ogg"):
                candidate = Path(str(stem) + ext)
                if candidate.exists():
                    return str(candidate)
        return None

    def _finalize(self, item: dict[str, Any], info: dict[str, Any], filepath: str) -> str:
        """Rename to match the playlist's metadata and write accurate tags."""
        cfg = self.cfg
        path = Path(filepath)

        desired_title = item.get("title") or info.get("title") or path.stem
        desired_artist = item.get("artist") or info.get("artist") or ""
        album = item.get("album") or info.get("album") or ""
        is_audio = path.suffix.lower() in {
            ".mp3", ".m4a", ".opus", ".flac", ".wav", ".ogg", ".aac", ".mp4", ".m4b",
        } and path.suffix.lower() != ".mp4"

        # A requested MP3 that is still WebM means conversion never happened.
        # Say so plainly instead of letting a raw video container pass as audio.
        requested_format = (cfg.get("audio_format") or "mp3").lower()
        if self._is_audio(item) and requested_format != "original" \
                and path.suffix.lower() in {".webm", ".mp4"}:
            self.hub.emit("log", {
                "level": "warning", "item_id": item["id"],
                "message": f"“{path.name}” kept its original {path.suffix} container — "
                           "FFmpeg could not convert it. Check the one-time setup.",
            })

        # Only rename when the playlist supplied better metadata than YouTube did.
        if cfg.get("rename_to_metadata", True) and is_audio and item.get("source") in ("spotify", "file", "compare"):
            if desired_artist:
                target_name = sanitize_filename(f"{desired_artist} - {desired_title}{path.suffix}")
                target = path.with_name(target_name)
                if target != path and not target.exists():
                    try:
                        path.rename(target)
                        path = target
                    except OSError:
                        pass

        if cfg.get("tag_metadata") and is_audio:
            cover_url = item.get("thumbnail") or _best_thumbnail(info)
            report = tagging.write_tags(
                path,
                title=desired_title,
                artist=desired_artist or None,
                album=album or None,
                albumartist=desired_artist or None,
                track_number=item.get("track_number"),
                cover_url=cover_url if cfg.get("embed_cover_art") else None,
                comment="Downloaded with Playlist Downloader",
            )
            if not report.get("ok"):
                self.hub.emit("log", {
                    "level": "warning", "item_id": item["id"],
                    "message": f"Tagged with warnings: {report.get('error')}",
                })

        if cfg.get("write_cover_file"):
            self._write_cover_file(path, item, info)

        return str(path)

    def _write_cover_file(self, path: Path, item: dict[str, Any], info: dict[str, Any]) -> None:
        url = item.get("thumbnail") or _best_thumbnail(info)
        if not url:
            return
        data = tagging.fetch_image(url)
        if not data:
            return
        ext = ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
        try:
            (path.parent / "cover" + ext).write_bytes(data)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Convenience for the API layer
# ---------------------------------------------------------------------------

def probe_url(url: str, cfg: Config) -> dict[str, Any]:
    """Fetch metadata for a single URL (used for previews and health checks)."""
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    opts.update(network_opts(cfg))
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "title": info.get("title"),
        "artist": info.get("artist") or info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "extractor": info.get("extractor"),
        "is_playlist": "entries" in info,
        "entry_count": len(list(info.get("entries") or [])) if "entries" in info else 1,
    }