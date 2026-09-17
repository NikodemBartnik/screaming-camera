from __future__ import annotations

from ..config import SpeakerConfig
from .base import Speaker


def create_speaker(cfg: SpeakerConfig, eufy_client=None) -> Speaker:
    if cfg.type == "local_audio":
        from .local_audio import LocalAudioSpeaker
        return LocalAudioSpeaker(cfg)
    if cfg.type == "remote_agent":
        from .remote_agent import RemoteAgentSpeaker
        return RemoteAgentSpeaker(cfg)
    if cfg.type == "eufy_talkback":
        if eufy_client is None:
            raise ValueError(f"speaker {cfg.id}: eufy_talkback requires eufy.enabled=true")
        from .eufy_talkback import EufyTalkbackSpeaker
        return EufyTalkbackSpeaker(cfg, eufy_client)
    raise ValueError(f"unknown speaker type {cfg.type}")


__all__ = ["Speaker", "create_speaker"]
