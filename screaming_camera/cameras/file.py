"""Video file (looped) or a directory of images - deterministic input for testing prompts
and the whole pipeline without cameras."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import cv2

from .base import CameraSource, Frame

log = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


class FileSource(CameraSource):
    async def run(self) -> None:
        path = Path(self.cfg.path)
        if not path.exists():
            self.status = "error"
            self.last_error = f"path not found: {path}"
            await self._stop.wait()
            return
        self.status = "streaming"
        interval = 1.0 / max(self.cfg.fps, 0.01)
        while not self.stopped:
            if path.is_dir():
                images = sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_EXT)
                if not images:
                    self.last_error = "no images in directory"
                    await asyncio.sleep(5)
                    continue
                for p in images:
                    if self.stopped:
                        break
                    img = cv2.imread(str(p))
                    if img is not None:
                        # Images in a folder are treated as "something happened" - each one is analysed.
                        self._put(Frame(self.cfg.id, img, trigger="file"))
                    await asyncio.sleep(interval)
            else:
                cap = cv2.VideoCapture(str(path))
                src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
                step = max(int(round(src_fps / max(self.cfg.fps, 0.01))), 1)
                idx = 0
                while not self.stopped:
                    ok, img = cap.read()
                    if not ok:
                        break
                    if idx % step == 0:
                        self._put(Frame(self.cfg.id, img))
                        await asyncio.sleep(interval)
                    idx += 1
                cap.release()
                await asyncio.sleep(1.0)
        self.status = "stopped"
