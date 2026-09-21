"""Speaks through the camera's own speaker via eufy-security-ws talkback.

eufy-security-client expects AAC frames in ADTS framing on the talkback stream, so the WAV is
transcoded with PyAV (16 kHz mono AAC-LC) and pushed in real-time-ish chunks.
NOTE: verified against the protocol, still to be confirmed on each physical camera model.
"""
from __future__ import annotations

import asyncio
import io
import logging
import wave

import av
import numpy as np

from ..eufy.ws_client import EufyWsClient
from .base import Speaker

log = logging.getLogger(__name__)

AAC_RATE = 16000
AAC_BITRATE = 20000  # higher bitrates stutter on Eufy devices (eufy-security-client issue #153)
FRAME_SECONDS = 1024 / AAC_RATE  # one AAC frame = 64 ms; the P2P layer stamps every write() as one frame


def wav_to_adts(wav: bytes, volume: float = 1.0, channels: int = 1) -> list[bytes]:
    """Return a list of ADTS packets (one per AAC frame, 1024 samples each)."""
    with wave.open(io.BytesIO(wav), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("expected 16-bit PCM wav")
    pcm = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch)
    if ch > 1:
        pcm = pcm.mean(axis=1).astype(np.int16).reshape(-1, 1)
    if volume != 1.0:
        pcm = np.clip(pcm.astype(np.float32) * volume, -32768, 32767).astype(np.int16)

    frame = av.AudioFrame.from_ndarray(pcm.T.copy(), format="s16", layout="mono")
    frame.sample_rate = rate
    layout = "stereo" if channels == 2 else "mono"
    resampler = av.AudioResampler(format="fltp", layout=layout, rate=AAC_RATE)

    out = io.BytesIO()
    container = av.open(out, mode="w", format="adts")
    try:
        stream = container.add_stream("aac", rate=AAC_RATE, layout=layout)
        stream.bit_rate = AAC_BITRATE
        for rf in resampler.resample(frame) + resampler.resample(None):
            for packet in stream.encode(rf):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    finally:
        container.close()
    # The muxer writes ADTS headers; split the byte stream into individual frames.
    return _split_adts(out.getvalue())


def _split_adts(data: bytes) -> list[bytes]:
    frames = []
    i = 0
    while i + 7 <= len(data):
        if data[i] == 0xFF and (data[i + 1] & 0xF0) == 0xF0:
            length = ((data[i + 3] & 0x03) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
            if length < 7:
                break
            frames.append(data[i:i + length])
            i += length
        else:
            i += 1
    return frames


class EufyTalkbackSpeaker(Speaker):
    def __init__(self, cfg, client: EufyWsClient):
        super().__init__(cfg)
        self.client = client
        self._lock = asyncio.Lock()

    async def play(self, wav: bytes) -> None:
        packets = await asyncio.to_thread(wav_to_adts, wav, self.cfg.volume, self.cfg.channels)
        duration = len(packets) * FRAME_SECONDS
        started_here = False
        async with self._lock:
            try:
                # Talkback only works while the camera's livestream is running.
                self.client.hold_stream(self.cfg.serial, duration + 20)
                started_here = await self.client.ensure_livestream(self.cfg.serial)
                await self.client.start_talkback(self.cfg.serial)  # returns once the station confirmed
                await asyncio.sleep(0.3)
                # Exactly one ADTS frame per command, paced in real time: eufy-security-client wraps each
                # write() in a frame header with a 64 ms timestamp step, so bigger chunks break playback.
                loop = asyncio.get_running_loop()
                t0 = loop.time()
                for i, frame in enumerate(packets):
                    await self.client.talkback_audio_data(self.cfg.serial, frame)
                    delay = t0 + (i + 1) * FRAME_SECONDS - loop.time()
                    if delay > 0:
                        await asyncio.sleep(delay)
                await asyncio.sleep(0.8)  # let the device drain its buffer before closing the session
                log.info("talkback %s: sent %d AAC frames (%.1fs)", self.cfg.id, len(packets), duration)
                self.status = "ok"
                self.last_error = ""
            except Exception as e:  # noqa: BLE001
                self.status = "error"
                self.last_error = str(e)
                raise
            finally:
                await self.client.stop_talkback(self.cfg.serial)
                self.client.stream_holds.pop(self.cfg.serial, None)
                if started_here:
                    await self.client.stop_livestream(self.cfg.serial)
