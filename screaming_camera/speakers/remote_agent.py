"""Plays through scripts/speaker_agent.py running on any Wi-Fi device with a speaker
(Raspberry Pi, old laptop, phone with Termux) - a DIY "Wi-Fi speaker"."""
from __future__ import annotations

import httpx

from .base import Speaker


class RemoteAgentSpeaker(Speaker):
    async def play(self, wav: bytes) -> None:
        url = self.cfg.url.rstrip("/") + "/play"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(url, content=wav, headers={"Content-Type": "audio/wav"},
                                      params={"volume": self.cfg.volume})
                r.raise_for_status()
            self.status = "ok"
            self.last_error = ""
        except Exception as e:  # noqa: BLE001
            self.status = "error"
            self.last_error = str(e)
            raise
