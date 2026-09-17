#!/usr/bin/env python3
"""Tiny "Wi-Fi speaker": run this on any device with a speaker (Raspberry Pi, old laptop, phone with
Termux) and point a `remote_agent` speaker at it. Single-file, only needs sounddevice + numpy.

    pip install sounddevice numpy
    python speaker_agent.py --port 8181 [--device "USB Audio"]

    curl -X POST --data-binary @hello.wav -H "Content-Type: audio/wav" http://pi:8181/play
"""
from __future__ import annotations

import argparse
import io
import json
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
import sounddevice as sd

DEVICE: int | None = None


def find_device(name: str) -> int | None:
    if not name:
        return None
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0 and name.lower() in d["name"].lower():
            return i
    raise SystemExit(f"no output device matching {name!r}")


def play_wav(data: bytes, volume: float) -> None:
    with wave.open(io.BytesIO(data), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}[width]
    pcm = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    pcm = (pcm - 128) / 128.0 if width == 1 else pcm / float(2 ** (8 * width - 1))
    if ch > 1:
        pcm = pcm.reshape(-1, ch)
    sd.play(np.clip(pcm * volume, -1, 1), samplerate=rate, device=DEVICE, blocking=True)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        self._json(200, {"ok": True, "device": DEVICE})

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path != "/play":
            return self._json(404, {"error": "use POST /play"})
        volume = float(parse_qs(url.query).get("volume", ["1"])[0])
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length)
        try:
            play_wav(data, volume)
            self._json(200, {"ok": True})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)})

    def log_message(self, fmt, *args):  # quieter
        print(self.address_string(), fmt % args)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8181)
    ap.add_argument("--device", default="", help="substring of the output device name")
    ap.add_argument("--list", action="store_true", help="list output devices and exit")
    args = ap.parse_args()
    if args.list:
        print(sd.query_devices())
        raise SystemExit
    DEVICE = find_device(args.device)
    print(f"speaker agent on 0.0.0.0:{args.port}, device={DEVICE if DEVICE is not None else 'default'}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
