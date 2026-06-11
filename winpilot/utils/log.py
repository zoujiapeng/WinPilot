"""Central event bus + logging: every agent/perception/action step publishes here.

The FastAPI WebSocket endpoint subscribes to push events to the browser in
real time; the trace recorder subscribes to persist them to JSONL. Events are
plain dicts: {"type": ..., "ts": ..., **payload}.
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from typing import Any, Callable

logger = logging.getLogger("winpilot")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class EventBus:
    """Thread-safe pub/sub. Subscribers get their own queue (non-blocking put)."""

    _MAX_QUEUE = 2000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._sinks: list[Callable[[dict[str, Any]], None]] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._MAX_QUEUE)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def add_sink(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Synchronous sink (e.g. trace recorder). Exceptions are swallowed+logged."""
        with self._lock:
            self._sinks.append(fn)

    def remove_sink(self, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if fn in self._sinks:
                self._sinks.remove(fn)

    def publish(self, event_type: str, **payload: Any) -> dict[str, Any]:
        event = {"type": event_type, "ts": round(time.time(), 3), **payload}
        with self._lock:
            subscribers = list(self._subscribers)
            sinks = list(self._sinks)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                logger.warning("事件队列已满，丢弃事件 %s", event_type)
        for sink in sinks:
            try:
                sink(event)
            except Exception:
                logger.exception("事件 sink 处理失败")
        return event


BUS = EventBus()
