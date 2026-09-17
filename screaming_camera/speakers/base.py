from __future__ import annotations

from ..config import SpeakerConfig


class Speaker:
    def __init__(self, cfg: SpeakerConfig):
        self.cfg = cfg
        self.status = "idle"
        self.last_error = ""

    async def play(self, wav: bytes) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    async def close(self) -> None:
        return None
