"""Any output device the OS knows about: USB, 3.5 mm, HDMI or a paired Bluetooth speaker
(after pairing, a BT speaker is just another sink for WASAPI / PipeWire).

``keep_alive`` plays a near-silent tone every ~20 s so Bluetooth speakers do not power down
and swallow the first second of the message."""
from __future__ import annotations

import asyncio
import io
import logging
import wave

import numpy as np
import sounddevice as sd

from .base import Speaker

log = logging.getLogger(__name__)

KEEP_ALIVE_INTERVAL = 20.0


def list_output_devices() -> list[dict]:
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_output_channels", 0) > 0:
            out.append({"index": i, "name": d["name"], "hostapi": sd.query_hostapis(d["hostapi"])["name"]})
    return out


def find_device(name_substring: str) -> int | None:
    if not name_substring:
        return None
    for d in list_output_devices():
        if name_substring.lower() in d["name"].lower():
            return d["index"]
    raise LookupError(f"no output device matching {name_substring!r}")


def wav_to_float(wav: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}[width]
    data = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if width == 1:
        data = (data - 128) / 128.0
    else:
        data /= float(2 ** (8 * width - 1))
    if ch > 1:
        data = data.reshape(-1, ch)
    return data, rate


class LocalAudioSpeaker(Speaker):
    def __init__(self, cfg):
        super().__init__(cfg)
        self._lock = asyncio.Lock()
        self._keep_alive_task: asyncio.Task | None = None
        if cfg.keep_alive:
            self._keep_alive_task = asyncio.create_task(self._keep_alive(), name=f"keepalive-{cfg.id}")

    def _play_sync(self, data: np.ndarray, rate: int) -> None:
        device = find_device(self.cfg.device)
        sd.play(data * float(self.cfg.volume), samplerate=rate, device=device, blocking=True)

    async def play(self, wav: bytes) -> None:
        data, rate = wav_to_float(wav)
        async with self._lock:
            try:
                await asyncio.to_thread(self._play_sync, data, rate)
                self.status = "ok"
                self.last_error = ""
            except Exception as e:  # noqa: BLE001
                self.status = "error"
                self.last_error = str(e)
                raise

    async def _keep_alive(self) -> None:
        rate = 16000
        tone = (np.sin(np.arange(int(rate * 0.5)) * 2 * np.pi * 50 / rate) * 0.002).astype(np.float32)
        while True:
            await asyncio.sleep(KEEP_ALIVE_INTERVAL)
            if self._lock.locked():
                continue
            try:
                await asyncio.to_thread(self._play_sync, tone, rate)
            except Exception as e:  # noqa: BLE001
                log.debug("keep-alive on %s failed: %s", self.cfg.id, e)

    async def close(self) -> None:
        if self._keep_alive_task:
            self._keep_alive_task.cancel()
