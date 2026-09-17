from __future__ import annotations

import asyncio

from ..config import CameraConfig
from .base import CameraSource, Frame


def create_source(cfg: CameraConfig, queue: asyncio.Queue, eufy_client=None) -> CameraSource:
    if cfg.type == "rtsp":
        from .rtsp import RtspSource
        return RtspSource(cfg, queue)
    if cfg.type == "webcam":
        from .webcam import WebcamSource
        return WebcamSource(cfg, queue)
    if cfg.type == "file":
        from .file import FileSource
        return FileSource(cfg, queue)
    if cfg.type == "eufy_p2p":
        if eufy_client is None:
            raise ValueError(f"camera {cfg.id}: eufy_p2p requires eufy.enabled=true")
        from .eufy_p2p import EufyP2PSource
        return EufyP2PSource(cfg, queue, eufy_client)
    raise ValueError(f"unknown camera type {cfg.type}")


__all__ = ["CameraSource", "Frame", "create_source"]
