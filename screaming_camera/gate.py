"""FrameGate: cheap first stage that decides which frames deserve a (slow) VLM call.

Continuous cameras: background-subtraction motion detector. Event-driven cameras: frames already
carry a trigger and pass straight through. Also keeps a short history so the analyzer can send
"what happened over the last seconds" instead of a single frame.
"""
from __future__ import annotations

import time
from collections import deque

import cv2
import numpy as np

from .cameras.base import Frame

WORK_WIDTH = 320


class FrameGate:
    def __init__(self, sensitivity: float, min_interval: float = 3.0, history: int = 4):
        self.sensitivity = sensitivity
        self.min_interval = min_interval
        self._bg: np.ndarray | None = None
        self._last_pass = 0.0
        self.history: deque[Frame] = deque(maxlen=history)
        self.last_motion = 0.0  # fraction of changed pixels in the last frame, for the UI

    def _small_gray(self, img: np.ndarray) -> np.ndarray:
        h, w = img.shape[:2]
        scale = WORK_WIDTH / max(w, 1)
        small = cv2.resize(img, (WORK_WIDTH, max(int(h * scale), 1)), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(gray, (9, 9), 0).astype(np.float32)

    def motion_fraction(self, img: np.ndarray) -> float:
        gray = self._small_gray(img)
        if self._bg is None:
            self._bg = gray
            return 0.0
        diff = cv2.absdiff(gray, self._bg)
        changed = float(np.count_nonzero(diff > 25)) / diff.size
        # Slow background update so a person standing still keeps counting as "changed" for a while.
        cv2.accumulateWeighted(gray, self._bg, 0.05)
        return changed

    def check(self, frame: Frame) -> str | None:
        """Return a trigger label if the frame should be analysed, else None."""
        self.history.append(frame)
        motion = self.motion_fraction(frame.image)
        self.last_motion = motion
        now = time.monotonic()
        if frame.trigger:
            self._last_pass = now
            return frame.trigger
        if motion >= self.sensitivity and now - self._last_pass >= self.min_interval:
            self._last_pass = now
            return "motion"
        return None

    def recent_frames(self, n: int) -> list[Frame]:
        return list(self.history)[-n:]
