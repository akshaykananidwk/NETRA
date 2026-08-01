"""Internal event bus.

Worker threads (vision, audio) publish events from their own threads;
the asyncio Orchestrator consumes them on the main event loop.
Publishing never blocks a worker — events are handed to the loop with
call_soon_threadsafe.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("krishna.events")

# Event type constants (mirrors the WebSocket event names)
CAMERA_STATUS = "camera.status"
MIC_STATUS = "mic.status"
MIC_LEVEL = "mic.level"
FACE_DETECTED = "face.detected"
FACE_RECOGNIZED = "face.recognized"
FACE_UNCERTAIN = "face.uncertain"
FACE_UNKNOWN = "face.unknown"
FACE_SEEN = "face.seen"          # periodic "still here" update per track
TRACK_LOST = "track.lost"
SYSTEM_STATUS = "system.status"
ERROR = "error"


@dataclass
class Event:
    type: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class EventBus:
    """asyncio.Queue bridged to worker threads."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, type: str, **data: Any) -> None:
        """Thread-safe, non-blocking publish. Drops the event if the queue is full."""
        evt = Event(type=type, data=data)
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._put_nowait, evt)
        except RuntimeError:
            pass  # loop shutting down

    def _put_nowait(self, evt: Event) -> None:
        try:
            self._queue.put_nowait(evt)
        except asyncio.QueueFull:
            log.warning("event bus full — dropping %s", evt.type)

    async def get(self) -> Event:
        return await self._queue.get()
