"""Configuration model. Everything the user can tune lives here and in config.yaml.

Nothing about *what* the system watches for or *how* it speaks is hard-coded:
prompts, thresholds, cameras and speakers all come from this file.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    """OpenAI-compatible VLM endpoint. GenieX on the IQ-9075, llama-server / Ollama on Windows."""
    endpoint: str = "http://127.0.0.1:11434/v1"
    name: str = "ministral-3:8b"
    # "openai": /chat/completions with image_url parts (GenieX, llama-server, Ollama, LM Studio).
    # "ollama": Ollama's native /api/chat (endpoint = http://host:11434) - more reliable images, think:false.
    api: Literal["openai", "ollama"] = "openai"
    api_key: str = ""
    # Extra JSON merged into every request, e.g. {"reasoning_effort": "none"} to stop Ollama's thinking models
    # from burning the token budget on reasoning.
    extra_body: dict = Field(default_factory=lambda: {"reasoning_effort": "none"})
    max_image_side: int = 768
    jpeg_quality: int = 85
    temperature: float = 0.4
    max_tokens: int = 400
    timeout_seconds: float = 120.0
    frames_per_request: int = 1  # >1 sends the last N gate-selected frames (motion context)
    # Generation speed (~13 tok/s on the IQ-9075 NPU) dominates latency, not image size. Two-stage mode
    # asks for a 15-token verdict first (~1.5 s) and only writes the full description + message when the
    # threat level reaches describe_min_threat (~4-5 s more).
    two_stage: bool = True
    describe_min_threat: int = 5


class CameraConfig(BaseModel):
    id: str
    name: str = ""
    type: Literal["rtsp", "eufy_p2p", "webcam", "file"] = "rtsp"
    enabled: bool = True
    # rtsp: paste the URL from the camera's app. Credentials may be in the URL, but putting them in
    # username/password is safer - they get percent-encoded, so @ : / # in a password still work.
    # Tapo: create a "camera account" in the Tapo app (Device settings -> Advanced -> Camera account),
    # then url = rtsp://<ip>:554/stream1 (full res) or /stream2 (720p, enough for the model).
    url: str = ""
    username: str = ""
    password: str = ""
    # webcam
    device_index: int = 0
    # file: video file or directory of images (looped) - for tests without hardware
    path: str = ""
    # eufy_p2p
    serial: str = ""
    # analysis
    fps: float = 2.0  # frames per second handed to the motion gate
    motion_sensitivity: float = 0.02  # fraction of changed pixels that counts as motion
    event_hold_seconds: float = 20.0  # eufy: keep the livestream alive this long after the trigger
    # Wait this long after a trigger before grabbing the frame to analyse - lets the person actually
    # enter the frame and do something instead of analysing the first blurry edge-of-frame moment.
    analysis_delay_seconds: float = 3.0
    speakers: list[str] = Field(default_factory=list)


class SpeakerConfig(BaseModel):
    id: str
    name: str = ""
    type: Literal["local_audio", "eufy_talkback", "tapo_talkback", "remote_agent"] = "local_audio"
    enabled: bool = True
    # local_audio: substring of the device name (sounddevice) or empty for default output
    device: str = ""
    keep_alive: bool = False  # play near-silence periodically so Bluetooth speakers do not sleep
    # eufy_talkback
    serial: str = ""
    channels: int = 1  # AAC channels for talkback: 1 = mono (most cams), 2 = stereo (some doorbells need it)
    # tapo_talkback: camera IP + the TP-Link *cloud account* password (the camera verifies a hash of
    # it locally; the RTSP camera account does not work for two-way audio). Stored in config.yaml.
    host: str = ""
    password: str = ""
    # remote_agent: http://host:port of scripts/speaker_agent.py
    url: str = ""
    volume: float = 1.0


class EufyConfig(BaseModel):
    enabled: bool = False
    ws_url: str = "ws://127.0.0.1:3000"
    # Motion events reach the bridge as Eufy push notifications, which depend on the HomeBase security
    # mode. Optionally switch every HomeBase to a mode when arming and back when disarming:
    # "" = don't touch, or one of away / home / disarmed / schedule / geo / custom1 / custom2 / custom3.
    guard_mode_on_arm: str = ""
    guard_mode_on_disarm: str = ""


class ScheduleWindow(BaseModel):
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])  # 0 = Monday
    start: str = "22:00"
    end: str = "06:00"


class PolicyConfig(BaseModel):
    armed: bool = False
    schedule_enabled: bool = False
    schedule: list[ScheduleWindow] = Field(default_factory=list)
    threat_threshold: int = 6  # 0-10, speak when threat_level >= threshold
    cooldown_seconds: float = 45.0  # per camera
    global_cooldown_seconds: float = 10.0  # between any two spoken messages
    analyze_when_disarmed: bool = True  # still run the model and log events, just stay quiet


class PromptConfig(BaseModel):
    persona: str = (
        "You are the AI security guard of a private house. You are observant, direct and a bit sarcastic. "
        "You watch camera frames and decide whether the situation is suspicious."
    )
    watch_for: str = (
        "People approaching the house, the door or the gate; people looking into windows; "
        "someone trying to open a door, a gate or a car; someone taking a package; "
        "people loitering or hiding; anyone present late at night."
    )
    ignore: str = (
        "Pets, wind moving plants, cars passing on the street, birds, shadows, "
        "delivery workers in uniform who leave a package and walk away."
    )
    message_style: str = (
        "Speak directly to the person as if through a loudspeaker. Refer to what they are wearing "
        "and what they are doing so they know they are being watched. Firm and a little witty. "
        "One or two short sentences."
    )
    language: str = "English"
    extra_instructions: str = ""
    max_message_words: int = 25  # each word is ~1.3 tokens at ~13 tok/s on the board


class TTSConfig(BaseModel):
    engine: Literal["piper", "pyttsx3", "none"] = "piper"
    piper_model: str = "en_US-ryan-high"  # voice name (auto-download) or path to .onnx
    piper_data_dir: str = "data/piper"
    speed: float = 1.0
    pyttsx3_voice: str = ""


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080


class StorageConfig(BaseModel):
    db_path: str = "data/events.sqlite"
    snapshots_dir: str = "data/snapshots"
    keep_days: int = 30


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    cameras: list[CameraConfig] = Field(default_factory=list)
    speakers: list[SpeakerConfig] = Field(default_factory=list)
    eufy: EufyConfig = Field(default_factory=EufyConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    def camera(self, camera_id: str) -> CameraConfig | None:
        return next((c for c in self.cameras if c.id == camera_id), None)

    def speaker(self, speaker_id: str) -> SpeakerConfig | None:
        return next((s for s in self.speakers if s.id == speaker_id), None)


DEFAULT_CONFIG_PATH = Path(os.environ.get("SC_CONFIG", "config.yaml"))


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    if not path.exists():
        cfg = AppConfig()
        save_config(cfg, path)
        return cfg
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig.model_validate(data)


def save_config(cfg: AppConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg.model_dump(mode="json"), f, sort_keys=False, allow_unicode=True, width=100)
    os.replace(tmp, path)


class ConfigStore:
    """Thread-safe holder of the live config with a version counter for hot-reload."""

    def __init__(self, path: Path = DEFAULT_CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._cfg = load_config(path)
        self.version = 1

    @property
    def cfg(self) -> AppConfig:
        return self._cfg

    def update(self, new_cfg: AppConfig) -> AppConfig:
        with self._lock:
            self._cfg = new_cfg
            self.version += 1
            save_config(new_cfg, self.path)
        return new_cfg

    def patch(self, **sections) -> AppConfig:
        """Replace whole top-level sections, e.g. patch(policy=PolicyConfig(...))."""
        data = self._cfg.model_dump()
        for key, value in sections.items():
            data[key] = value.model_dump() if isinstance(value, BaseModel) else value
        return self.update(AppConfig.model_validate(data))
