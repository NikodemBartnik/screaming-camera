"""Sound the Eufy siren (HomeBase or camera) instead of speaking.

eufyCam 3 and friends cannot do talkback through eufy-security-client: Station.startTalkback has no
command branch for that device family, so it only opens a local audio stream and the camera never
opens its speaker (the session looks healthy, nothing is audible). The siren *is* supported, so a
camera whose speaker we cannot drive can still make noise at an intruder - usually alongside a
spoken message on another speaker.
"""
from __future__ import annotations

import asyncio
import logging

from ..eufy.ws_client import EufyWsClient
from .base import Speaker

log = logging.getLogger(__name__)


class EufyAlarmSpeaker(Speaker):
    def __init__(self, cfg, client: EufyWsClient):
        super().__init__(cfg)
        self.client = client
        self._lock = asyncio.Lock()

    async def play(self, wav: bytes) -> None:  # noqa: ARG002 - a siren has nothing to say
        if not self.cfg.serial:
            raise ValueError(f"speaker {self.cfg.id}: set the HomeBase or camera serial")
        seconds = max(int(self.cfg.alarm_seconds), 1)
        async with self._lock:
            try:
                await self.client.trigger_alarm(self.cfg.serial, seconds)
                self.status = "ok"
                self.last_error = ""
                log.info("eufy siren %s: sounding for %ds", self.cfg.id, seconds)
            except Exception as e:  # noqa: BLE001
                self.status = "error"
                self.last_error = str(e)
                raise

    async def stop_alarm(self) -> None:
        await self.client.reset_alarm(self.cfg.serial)
