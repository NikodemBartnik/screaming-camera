"""Event-driven Eufy camera (battery cams, doorbells) through eufy-security-ws.

Flow: HomeBase reports motion/person/ring -> we start the P2P livestream -> raw H.264/H.265 chunks
arrive over the websocket -> PyAV decodes them -> frames go to the pipeline. The stream is kept
alive ``event_hold_seconds`` after the last trigger, then stopped to save the battery.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any

import av

from ..eufy.ws_client import EufyWsClient, decode_buffer
from .base import CameraSource, Frame, FrameThrottle

log = logging.getLogger(__name__)

CODEC_MAP = {"h264": "h264", "h265": "hevc", "hevc": "hevc"}
TRIGGER_EVENTS = {"motion detected": "motion", "person detected": "person", "rings": "ring",
                  "stranger person detected": "stranger", "vehicle detected": "vehicle",
                  "package delivered": "package", "package taken": "package_taken"}


class _ChunkReader:
    """File-like object PyAV can demux from; fed by websocket events, ends on close()."""

    def __init__(self):
        self._q: queue.Queue[bytes | None] = queue.Queue()
        self._buf = b""
        self._eof = False

    def feed(self, data: bytes) -> None:
        self._q.put(data)

    def close(self) -> None:
        self._q.put(None)

    def read(self, n: int = -1) -> bytes:
        while len(self._buf) < n and not self._eof:
            try:
                chunk = self._q.get(timeout=10)
            except queue.Empty:
                break
            if chunk is None:
                self._eof = True
                break
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


class EufyP2PSource(CameraSource):
    def __init__(self, cfg, queue_, client: EufyWsClient):
        super().__init__(cfg, queue_)
        self.client = client
        self._reader: _ChunkReader | None = None
        self._decoder: threading.Thread | None = None
        self._codec = "h264"
        self._hold_until = 0.0
        self._pending_trigger: str | None = None
        self._starting = False
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        for name in TRIGGER_EVENTS:
            self.client.on("device", name, self._on_trigger)
        self.client.on("device", "livestream started", self._on_started)
        self.client.on("device", "livestream video data", self._on_video)
        self.client.on("device", "livestream stopped", self._on_stopped)
        self.status = "idle"
        try:
            while not self.stopped:
                await asyncio.sleep(1)
                if self._reader is not None and time.monotonic() > self._hold_until:
                    log.info("camera %s: hold expired, stopping livestream", self.cfg.id)
                    await self.client.stop_livestream(self.cfg.serial)
                    self._close_reader()
        finally:
            if self._reader is not None:
                await self.client.stop_livestream(self.cfg.serial)
                self._close_reader()
            self.status = "stopped"

    async def trigger(self, label: str = "manual") -> None:
        """Wake the camera as if the HomeBase had reported an event (used by the UI test button)."""
        await self._on_trigger({"serialNumber": self.cfg.serial, "event": label, "state": True})

    # ---- eufy events -----------------------------------------------------------------------
    async def _on_trigger(self, ev: dict[str, Any]) -> None:
        if ev.get("serialNumber") != self.cfg.serial or ev.get("state") is False:
            return
        label = TRIGGER_EVENTS.get(ev.get("event", ""), ev.get("event", "event"))
        self._hold_until = time.monotonic() + self.cfg.event_hold_seconds
        self._pending_trigger = label
        if self._reader is None and not self._starting:
            self._starting = True
            self.status = "waking"
            log.info("camera %s: %s -> starting livestream", self.cfg.id, label)
            try:
                await self.client.start_livestream(self.cfg.serial)
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                self.status = "error"
                log.warning("camera %s: start_livestream failed: %s", self.cfg.id, e)
                self._starting = False

    def _on_started(self, ev: dict[str, Any]) -> None:
        if ev.get("serialNumber") != self.cfg.serial:
            return
        self._starting = False
        self._close_reader()
        self._reader = _ChunkReader()
        self.status = "streaming"
        self.last_error = ""

    def _on_video(self, ev: dict[str, Any]) -> None:
        if ev.get("serialNumber") != self.cfg.serial:
            return
        if self._reader is None:  # data before "started" event - create on the fly
            self._on_started(ev)
        meta = ev.get("metadata") or {}
        codec = str(meta.get("videoCodec", "h264")).lower()
        self._codec = CODEC_MAP.get(codec, "h264")
        assert self._reader is not None
        self._reader.feed(decode_buffer(ev.get("buffer")))
        if self._decoder is None or not self._decoder.is_alive():
            self._decoder = threading.Thread(target=self._decode, args=(self._reader, self._codec),
                                             name=f"eufy-dec-{self.cfg.id}", daemon=True)
            self._decoder.start()

    def _on_stopped(self, ev: dict[str, Any]) -> None:
        if ev.get("serialNumber") != self.cfg.serial:
            return
        self._close_reader()
        self.status = "idle"

    def _close_reader(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    # ---- decoding --------------------------------------------------------------------------
    def _decode(self, reader: _ChunkReader, codec: str) -> None:
        throttle = FrameThrottle(self.cfg.fps)
        try:
            with av.open(reader, format=codec, mode="r") as container:
                stream = container.streams.video[0]
                stream.thread_type = "AUTO"
                for frame in container.decode(stream):
                    if self.stopped:
                        break
                    if not throttle.allow() and self._pending_trigger is None:
                        continue
                    img = frame.to_ndarray(format="bgr24")
                    trig, self._pending_trigger = self._pending_trigger, None
                    assert self._loop is not None
                    self._loop.call_soon_threadsafe(self._put, Frame(self.cfg.id, img, trigger=trig))
        except Exception as e:  # noqa: BLE001
            if reader is self._reader:  # unexpected - otherwise it's just the stream ending
                self.last_error = f"decode: {e}"
                log.warning("camera %s: decode error: %s", self.cfg.id, e)
