#!/usr/bin/env python3
"""Check an RTSP URL before adding it to config: prints codec/resolution/fps and saves one frame.

    python scripts/probe_rtsp.py rtsp://user:pass@192.168.1.50/stream2
    python scripts/probe_rtsp.py rtsp://192.168.1.60/live0 --out samples/s350.jpg
"""
from __future__ import annotations

import argparse
import sys
import time

import av


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out", default="probe.jpg", help="where to save the first decoded frame")
    ap.add_argument("--seconds", type=float, default=5.0, help="how long to read to measure fps")
    args = ap.parse_args()

    print(f"connecting to {args.url} ...")
    t0 = time.time()
    try:
        container = av.open(args.url, options={"rtsp_transport": "tcp", "stimeout": "10000000"}, timeout=15)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"FAILED: {e}\n  - wrong user/password or URL path?\n  - RTSP/NAS not enabled in the camera app?\n  - camera on another VLAN / firewall?")
    stream = container.streams.video[0]
    print(f"connected in {time.time() - t0:.1f}s: codec={stream.codec_context.name} "
          f"{stream.codec_context.width}x{stream.codec_context.height} declared fps={stream.average_rate}")
    n, first = 0, None
    t1 = time.time()
    for frame in container.decode(stream):
        if first is None:
            first = frame.to_image()
            first.save(args.out)
            print(f"first frame after {time.time() - t0:.1f}s -> saved {args.out}")
        n += 1
        if time.time() - t1 > args.seconds:
            break
    print(f"measured ~{n / max(time.time() - t1, 0.01):.1f} fps over {args.seconds}s. OK - use this URL as a `rtsp` camera.")
    container.close()


if __name__ == "__main__":
    main()
