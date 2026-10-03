"""Guards the eufy_p2p source against the stray-chunk loop seen on the T8170 workshop camera:
stop_livestream -> camera keeps sending video -> reader resurrected -> stopped again, 1 Hz forever."""
from __future__ import annotations

import asyncio
import time

import pytest

from screaming_camera.cameras.eufy_p2p import EufyP2PSource
from screaming_camera.config import CameraConfig


class FakeClient:
    """Only what the source touches."""

    def __init__(self):
        self.handlers = {}
        self.stopped: list[str] = []

    def on(self, source, event, handler):
        self.handlers[f"{source}:{event}"] = handler

    def station_of(self, serial):
        return "STATION"

    def station_lock(self, serial):
        return asyncio.Lock()

    def stream_held(self, serial):
        return False

    async def start_livestream(self, serial):
        return None

    async def stop_livestream(self, serial):
        self.stopped.append(serial)


def _source() -> tuple[EufyP2PSource, FakeClient]:
    client = FakeClient()
    cfg = CameraConfig(id="workshop", type="eufy_p2p", serial="T8170TEST", event_hold_seconds=20)
    src = EufyP2PSource(cfg, asyncio.Queue(maxsize=4), client)
    return src, client


def _video_event(size: int = 64) -> dict:
    return {"serialNumber": "T8170TEST", "buffer": [0] * size,
            "metadata": {"videoCodec": "H265", "videoWidth": 2880, "videoHeight": 1616, "videoFPS": 15}}


def test_stray_chunks_after_stop_do_not_restart_the_stream():
    src, _ = _source()
    src._hold_until = time.monotonic() - 1      # the hold has expired and we stopped the stream
    src._starting = False
    src._on_video(_video_event())
    assert src._reader is None, "a late chunk must not resurrect the stream"


def test_chunks_during_an_event_are_accepted():
    src, _ = _source()
    src._loop = asyncio.new_event_loop()
    try:
        src._hold_until = time.monotonic() + 10  # an event is in progress
        src._on_video(_video_event())
        assert src._reader is not None, "video during an event must be decoded"
    finally:
        src._loop.close()


def test_chunks_while_waking_are_accepted():
    src, _ = _source()
    src._loop = asyncio.new_event_loop()
    try:
        src._hold_until = 0.0
        src._starting = True                     # start_livestream sent, data can precede the event
        src._on_video(_video_event())
        assert src._reader is not None
    finally:
        src._loop.close()


@pytest.mark.asyncio
async def test_trigger_opens_the_window_again():
    src, client = _source()
    src._loop = asyncio.get_running_loop()
    src._hold_until = time.monotonic() - 1
    src._on_video(_video_event())
    assert src._reader is None
    await src.trigger("manual")                  # a new event re-opens the window
    src._starting = False
    src._on_video(_video_event())
    assert src._reader is not None
