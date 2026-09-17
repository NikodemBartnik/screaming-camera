"""Camera source interface.

A source pushes ``Frame`` objects into an asyncio queue. Continuous sources (RTSP, webcam, file)
push frames at ``fps``; event-driven sources (Eufy P2P) push frames only while a livestream is
running and tag the first ones with the trigger that woke the camera ("motion", "person", "ring").
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import numpy as np

from ..config import CameraConfig


@dataclass
class Frame:
    camera_id: str
    image: np.ndarray  # BGR uint8
    ts: float = field(default_factory=time.time)
    trigger: str | None = None  # set by event-driven sources; None means "gate decides"


class CameraSource:
    def __init__(self, cfg: CameraConfig, queue: asyncio.Queue[Frame]):
        self.cfg = cfg
        self.queue = queue
        self.status: str = "starting"
        self.last_error: str = ""
        self._stop = asyncio.Event()

    async def run(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _put(self, frame: Frame) -> None:
        """Drop the oldest frame if the consumer is behind - analysis is slow, never let a backlog form."""
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(frame)


class FrameThrottle:
    """Lets at most ``fps`` frames per second through."""

    def __init__(self, fps: float):
        self.interval = 1.0 / max(fps, 0.01)
        self._last = 0.0

    def allow(self) -> bool:
        now = time.monotonic()
        if now - self._last >= self.interval:
            self._last = now
            return True
        return False
