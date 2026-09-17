"""Decides whether a verdict turns into a spoken message: armed state, schedule, threshold, cooldowns."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from .analyzer import Analysis
from .config import PolicyConfig


@dataclass
class Decision:
    speak: bool
    reason: str


def _in_window(now: datetime, start: str, end: str, days: list[int]) -> bool:
    t = now.strftime("%H:%M")
    if start <= end:
        return now.weekday() in days and start <= t < end
    # Window crosses midnight: the evening part belongs to today, the morning part to yesterday's window.
    if t >= start:
        return now.weekday() in days
    if t < end:
        return (now.weekday() - 1) % 7 in days
    return False


class Policy:
    def __init__(self, cfg: PolicyConfig):
        self.cfg = cfg
        self._last_spoken: dict[str, float] = {}
        self._last_global = 0.0

    def update(self, cfg: PolicyConfig) -> None:
        self.cfg = cfg

    def is_armed(self, now: datetime | None = None) -> bool:
        if self.cfg.armed:
            return True
        if self.cfg.schedule_enabled:
            now = now or datetime.now()
            return any(_in_window(now, w.start, w.end, w.days) for w in self.cfg.schedule)
        return False

    def decide(self, camera_id: str, analysis: Analysis) -> Decision:
        if analysis.error:
            return Decision(False, f"analysis error: {analysis.error}")
        if not self.is_armed():
            return Decision(False, "disarmed")
        if analysis.threat_level < self.cfg.threat_threshold:
            return Decision(False, f"threat {analysis.threat_level} < threshold {self.cfg.threat_threshold}")
        if not analysis.message.strip():
            return Decision(False, "model returned no message")
        now = time.monotonic()
        if now - self._last_global < self.cfg.global_cooldown_seconds:
            return Decision(False, "global cooldown")
        last = self._last_spoken.get(camera_id, 0.0)
        if now - last < self.cfg.cooldown_seconds:
            return Decision(False, f"camera cooldown ({int(self.cfg.cooldown_seconds - (now - last))}s left)")
        return Decision(True, "armed, threat above threshold")

    def mark_spoken(self, camera_id: str) -> None:
        now = time.monotonic()
        self._last_spoken[camera_id] = now
        self._last_global = now
