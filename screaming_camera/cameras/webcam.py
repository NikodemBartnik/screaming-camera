"""Local webcam via OpenCV - handy for developing on a laptop without any IP camera."""
from __future__ import annotations

import asyncio
import logging
import threading
import time

import cv2

from .base import CameraSource, Frame, FrameThrottle

log = logging.getLogger(__name__)


class WebcamSource(CameraSource):
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        threading.Thread(target=self._reader, args=(loop,), name=f"webcam-{self.cfg.id}", daemon=True).start()
        await self._stop.wait()

    def _reader(self, loop: asyncio.AbstractEventLoop) -> None:
        throttle = FrameThrottle(self.cfg.fps)
        while not self.stopped:
            cap = cv2.VideoCapture(self.cfg.device_index)
            if not cap.isOpened():
                self.status = "error"
                self.last_error = f"cannot open webcam {self.cfg.device_index}"
                time.sleep(5)
                continue
            self.status = "streaming"
            self.last_error = ""
            while not self.stopped:
                ok, img = cap.read()
                if not ok:
                    self.last_error = "read failed"
                    break
                if throttle.allow():
                    loop.call_soon_threadsafe(self._put, Frame(self.cfg.id, img))
                else:
                    time.sleep(0.005)
            cap.release()
        self.status = "stopped"
