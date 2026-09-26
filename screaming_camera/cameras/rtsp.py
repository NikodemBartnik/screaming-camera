"""RTSP / any URL that PyAV (ffmpeg) can open. Works for Tapo, wired Eufy cams with NAS/RTSP
enabled, Reolink, generic ONVIF cameras... Decoding runs in a thread; reconnects forever."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from urllib.parse import quote, urlsplit, urlunsplit

import av

from .base import CameraSource, Frame, FrameThrottle

log = logging.getLogger(__name__)

RECONNECT_DELAY = 5.0
MAX_RECONNECT_DELAY = 60.0
OPEN_OPTIONS = {"rtsp_transport": "tcp", "stimeout": "5000000", "max_delay": "500000"}


def build_url(url: str, username: str = "", password: str = "") -> str:
    """Put credentials into the URL, percent-encoded. Credentials already in the URL win."""
    url = url.strip()
    if not username or "@" in urlsplit(url).netloc:
        return url
    parts = urlsplit(url if "://" in url else "rtsp://" + url)
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def explain_error(e: Exception) -> str:
    """Turn ffmpeg's terse errors into something actionable in the panel."""
    msg = str(e)
    low = msg.lower()
    if "401" in msg or "unauthorized" in low:
        return ("Camera rejected the credentials (401). For Tapo/TP-Link use the *camera account* "
                "created in the app (Device settings -> Advanced settings -> Camera account), not your cloud login.")
    if "404" in msg or "not found" in low:
        return "Stream path not found (404) - try /stream1 or /stream2 (Tapo), /live0 (Eufy), or check the app for the exact path."
    if "timed out" in low or "timeout" in low or "immediate exit" in low:
        return "No response - wrong IP/port, camera off, RTSP disabled in the app, or a different VLAN/firewall."
    if "connection refused" in low:
        return "Connection refused - RTSP is disabled on the camera or the port is wrong."
    return msg


def probe(url: str, username: str = "", password: str = "", seconds: float = 3.0) -> dict:
    """Open the stream briefly and report what it carries (used by the panel's Test button)."""
    full = build_url(url, username, password)
    t0 = time.monotonic()
    container = None
    try:
        container = av.open(full, options=OPEN_OPTIONS, timeout=10)
        stream = container.streams.video[0]
        info = {"ok": True, "codec": stream.codec_context.name,
                "width": stream.codec_context.width, "height": stream.codec_context.height,
                "declared_fps": float(stream.average_rate) if stream.average_rate else None,
                "connect_ms": int((time.monotonic() - t0) * 1000)}
        n, t1 = 0, time.monotonic()
        for _ in container.decode(stream):
            n += 1
            if info.get("first_frame_ms") is None:
                info["first_frame_ms"] = int((time.monotonic() - t0) * 1000)
            if time.monotonic() - t1 > seconds:
                break
        info["measured_fps"] = round(n / max(time.monotonic() - t1, 0.01), 1)
        return info
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": explain_error(e), "raw_error": str(e)}
    finally:
        if container is not None:
            try:
                container.close()
            except Exception:  # noqa: BLE001
                pass


class RtspSource(CameraSource):
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        thread = threading.Thread(target=self._reader, args=(loop,), name=f"rtsp-{self.cfg.id}", daemon=True)
        thread.start()
        await self._stop.wait()

    def _reader(self, loop: asyncio.AbstractEventLoop) -> None:
        throttle = FrameThrottle(self.cfg.fps)
        delay = RECONNECT_DELAY
        while not self.stopped:
            container = None
            try:
                self.status = "connecting"
                container = av.open(build_url(self.cfg.url, self.cfg.username, self.cfg.password),
                                    options=OPEN_OPTIONS, timeout=10)
                stream = container.streams.video[0]
                stream.thread_type = "AUTO"
                # We only need a couple of frames per second: skip non-key frames when far behind.
                self.status = "streaming"
                self.last_error = ""
                delay = RECONNECT_DELAY
                for packet in container.demux(stream):
                    if self.stopped:
                        break
                    for frame in packet.decode():
                        if not throttle.allow():
                            continue
                        img = frame.to_ndarray(format="bgr24")
                        loop.call_soon_threadsafe(self._put, Frame(self.cfg.id, img))
            except Exception as e:  # noqa: BLE001 - any ffmpeg failure -> reconnect
                self.last_error = explain_error(e)
                self.status = "error"
                log.warning("camera %s: %s (reconnecting in %ss)", self.cfg.id, self.last_error, delay)
                time.sleep(delay)
                # Back off on persistent failures (bad credentials, camera off) so the log stays readable.
                delay = min(delay * 2, MAX_RECONNECT_DELAY)
            finally:
                if container is not None:
                    try:
                        container.close()
                    except Exception:  # noqa: BLE001
                        pass
        self.status = "stopped"
