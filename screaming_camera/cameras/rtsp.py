"""RTSP / any URL that PyAV (ffmpeg) can open. Works for Tapo, wired Eufy cams with NAS/RTSP
enabled, Reolink, generic ONVIF cameras... Decoding runs in a thread; reconnects forever."""
from __future__ import annotations

import asyncio
import logging
import threading
import time

import av

from .base import CameraSource, Frame, FrameThrottle

log = logging.getLogger(__name__)

RECONNECT_DELAY = 5.0


class RtspSource(CameraSource):
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        thread = threading.Thread(target=self._reader, args=(loop,), name=f"rtsp-{self.cfg.id}", daemon=True)
        thread.start()
        await self._stop.wait()

    def _reader(self, loop: asyncio.AbstractEventLoop) -> None:
        throttle = FrameThrottle(self.cfg.fps)
        while not self.stopped:
            container = None
            try:
                self.status = "connecting"
                container = av.open(
                    self.cfg.url,
                    options={"rtsp_transport": "tcp", "stimeout": "5000000", "max_delay": "500000"},
                    timeout=10,
                )
                stream = container.streams.video[0]
                stream.thread_type = "AUTO"
                # We only need a couple of frames per second: skip non-key frames when far behind.
                self.status = "streaming"
                self.last_error = ""
                for packet in container.demux(stream):
                    if self.stopped:
                        break
                    for frame in packet.decode():
                        if not throttle.allow():
                            continue
                        img = frame.to_ndarray(format="bgr24")
                        loop.call_soon_threadsafe(self._put, Frame(self.cfg.id, img))
            except Exception as e:  # noqa: BLE001 - any ffmpeg failure -> reconnect
                self.last_error = str(e)
                self.status = "error"
                log.warning("camera %s: %s (reconnecting in %ss)", self.cfg.id, e, RECONNECT_DELAY)
                time.sleep(RECONNECT_DELAY)
            finally:
                if container is not None:
                    try:
                        container.close()
                    except Exception:  # noqa: BLE001
                        pass
        self.status = "stopped"
