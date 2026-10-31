"""Tiny pub/sub hub used to stream live progress to the browser over SSE."""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Callable


class EventHub:
    """Broadcasts typed events to open SSE connections.

    Events are dicts: {"type": str, "data": dict, "ts": float}.
    Subscribers can filter by event type via the `types` frozenset.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[dict[str, Any]] = []
        self._sub_id = 0
        self._listeners: list[Callable[[str, dict], None]] = []

    # -- emitting ----------------------------------------------------------
    def emit(self, event_type: str, data: dict | None = None) -> None:
        event = {"type": event_type, "data": data or {}, "ts": time.time()}
        payload = json.dumps(event, default=str)
        dead: list[dict[str, Any]] = []
        with self._lock:
            for sub in self._subscribers:
                if event_type in sub["types"]:
                    try:
                        sub["queue"].put_nowait(payload)
                    except queue.Full:
                        dead.append(sub)
            for sub in dead:
                self._subscribers.remove(sub)
        for listener in list(self._listeners):
            try:
                listener(event_type, data or {})
            except Exception:
                pass

    def on(self, event_type: str, callback: Callable[[str, dict], None]) -> None:
        self._listeners.append(callback)

    # -- subscribing -------------------------------------------------------
    def subscribe(self, types: frozenset[str] | None = None, maxsize: int = 500) -> int:
        """Register a subscriber; returns an id to be passed to `pump`."""
        sub = {
            "id": self._sub_id,
            "queue": queue.Queue(maxsize=maxsize),
            "types": types or frozenset(),
        }
        self._sub_id += 1
        with self._lock:
            self._subscribers.append(sub)
        return sub["id"]

    def pump(self, sub_id: int, timeout: float = 15.0) -> str | None:
        """Block for an event, returning None on timeout / missing subscriber."""
        with self._lock:
            sub = next(
                (s for s in self._subscribers if s["id"] == sub_id), None
            )
        if sub is None:
            return None
        try:
            return sub["queue"].get(timeout=timeout)
        except queue.Empty:
            return None

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s["id"] != sub_id]


_hub = EventHub()


def get_hub() -> EventHub:
    return _hub