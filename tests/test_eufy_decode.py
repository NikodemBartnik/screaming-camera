"""The P2P decoder must emit frames while the stream is still running (low-latency), not only at EOF."""
from __future__ import annotations

import asyncio
import io
import threading
import time

import av
import numpy as np
import pytest

from screaming_camera.cameras.eufy_p2p import EufyP2PSource, _ChunkReader
from screaming_camera.config import CameraConfig


def make_h264_stream(frames: int = 40, size=(320, 240)) -> bytes:
    """Raw Annex-B H.264 elementary stream, like eufy-security-ws delivers."""
    buf = io.BytesIO()
    with av.open(buf, mode="w", format="h264") as out:
        st = out.add_stream("libx264", rate=10)
        st.width, st.height, st.pix_fmt = size[0], size[1], "yuv420p"
        st.options = {"tune": "zerolatency", "preset": "ultrafast", "g": "10"}
        rng = np.random.default_rng(0)
        for i in range(frames):
            # noisy frames so the stream has a realistic size (a few hundred KB, many chunks)
            img = rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            for pkt in st.encode(frame):
                out.mux(pkt)
        for pkt in st.encode(None):
            out.mux(pkt)
    return buf.getvalue()


class _Client:  # only what EufyP2PSource touches during decoding
    def on(self, *a): ...


async def test_frames_arrive_before_stream_ends():
    data = make_h264_stream()
    chunks = [data[i:i + 3000] for i in range(0, len(data), 3000)]
    assert len(chunks) > 8, "need a multi-chunk stream for this test"

    q: asyncio.Queue = asyncio.Queue(maxsize=4)
    src = EufyP2PSource(CameraConfig(id="t", type="eufy_p2p", serial="X", fps=100), q, _Client())
    src._loop = asyncio.get_running_loop()
    reader = _ChunkReader()
    src._reader = reader
    threading.Thread(target=src._decode, args=(reader, "h264"), daemon=True).start()

    # Feed only the first half slowly and expect a frame long before the rest (and EOF) arrives.
    half = len(chunks) // 2
    for c in chunks[:half]:
        reader.feed(c)
        await asyncio.sleep(0.02)
    t0 = time.monotonic()
    frame = await asyncio.wait_for(q.get(), timeout=3.0)
    assert frame.image.shape == (240, 320, 3)
    assert time.monotonic() - t0 < 3.0
    for c in chunks[half:]:
        reader.feed(c)
    reader.close()
    await asyncio.sleep(0.5)
    assert src.last_error == ""


def test_chunk_reader_short_reads():
    r = _ChunkReader()
    r.feed(b"abc")
    assert r.read(1000) == b"abc"  # returns what it has, does not wait for 1000 bytes
    r.close()
    assert r.read(10) == b""
