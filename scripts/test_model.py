#!/usr/bin/env python3
"""Day-0 spike: send one image to the model endpoint and time it. No cameras needed.

    python scripts/test_model.py samples/person.jpg
    python scripts/test_model.py samples/person.jpg --endpoint http://127.0.0.1:18181/v1 --model gemma-4-E4B-it
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402

from screaming_camera.analyzer import Analyzer  # noqa: E402
from screaming_camera.config import ModelConfig, PromptConfig, load_config  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--endpoint")
    ap.add_argument("--model")
    ap.add_argument("--max-side", type=int)
    ap.add_argument("-n", type=int, default=1, help="repeat N times (first call includes model load)")
    args = ap.parse_args()

    cfg = load_config(Path(args.config)) if Path(args.config).exists() else None
    model = cfg.model if cfg else ModelConfig()
    prompt = cfg.prompt if cfg else PromptConfig()
    if args.endpoint:
        model.endpoint = args.endpoint
    if args.model:
        model.name = args.model
    if args.max_side:
        model.max_image_side = args.max_side

    img = cv2.imread(args.image)
    if img is None:
        raise SystemExit(f"cannot read {args.image}")
    a = Analyzer(model, prompt)
    print("endpoint:", model.endpoint, "model:", model.name, "health:", await a.health())
    for i in range(args.n):
        r = await a.analyze([img], "test camera", "manual test")
        print(f"\n--- run {i + 1}: {r.latency_ms} ms")
        print(json.dumps(r.model_dump(exclude={"raw"}), indent=2, ensure_ascii=False))
        if r.error:
            print("RAW:", r.raw[:500])


if __name__ == "__main__":
    asyncio.run(main())
