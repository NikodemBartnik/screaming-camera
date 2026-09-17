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
AAC_BITRATE = 32000
CHUNK_SECONDS = 0.25


def wav_to_adts(wav: bytes, volume: float = 1.0) -> list[bytes]:
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
    resampler = av.AudioResampler(format="fltp", layout="mono", rate=AAC_RATE)

    out = io.BytesIO()
    container = av.open(out, mode="w", format="adts")
    try:
        stream = container.add_stream("aac", rate=AAC_RATE, layout="mono")
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
        packets = await asyncio.to_thread(wav_to_adts, wav, self.cfg.volume)
        frames_per_chunk = max(int(CHUNK_SECONDS * AAC_RATE / 1024), 1)
        async with self._lock:
            try:
                await self.client.start_talkback(self.cfg.serial)
                await asyncio.sleep(0.5)  # let the station open the audio channel
                for i in range(0, len(packets), frames_per_chunk):
                    chunk = b"".join(packets[i:i + frames_per_chunk])
                    await self.client.talkback_audio_data(self.cfg.serial, chunk)
                    await asyncio.sleep(CHUNK_SECONDS * 0.9)
                await asyncio.sleep(0.5)
                self.status = "ok"
                self.last_error = ""
            except Exception as e:  # noqa: BLE001
                self.status = "error"
                self.last_error = str(e)
                raise
            finally:
                await self.client.stop_talkback(self.cfg.serial)
