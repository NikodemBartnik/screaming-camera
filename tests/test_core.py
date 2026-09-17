"""Hardware-free tests for the parts that have logic worth guarding."""
from __future__ import annotations

import io
import time
import wave
from datetime import datetime

import numpy as np

from screaming_camera.analyzer import Analysis, build_system_prompt, encode_image, parse_analysis
from screaming_camera.cameras.base import Frame
from screaming_camera.config import AppConfig, PolicyConfig, PromptConfig, ScheduleWindow
from screaming_camera.eufy.ws_client import decode_buffer
from screaming_camera.gate import FrameGate
from screaming_camera.policy import Policy
from screaming_camera.speakers.eufy_talkback import wav_to_adts


# ---- analyzer ---------------------------------------------------------------------------------
def test_parse_plain_json():
    a = parse_analysis('{"threat_level": 7, "scene": "x", "people": [{"clothing": "red jacket", "action": "opening gate"}], "message": "Hey"}')
    assert a.threat_level == 7 and a.people[0].clothing == "red jacket" and a.message == "Hey" and not a.error


def test_parse_fenced_and_prose():
    text = 'Sure! Here is the analysis:\n```json\n{"threat_level": "3", "scene": "cat", "people": [], "message": ""}\n```\nHope this helps.'
    a = parse_analysis(text)
    assert a.threat_level == 3 and a.scene == "cat" and a.message == "" and not a.error


def test_parse_garbage():
    a = parse_analysis("I cannot see anything.")
    assert a.error and a.threat_level == 0


def test_parse_clamps_threat():
    assert parse_analysis('{"threat_level": 42}').threat_level == 10
    assert parse_analysis('{"threat_level": -3}').threat_level == 0


def test_prompt_contains_user_settings():
    p = PromptConfig(persona="Grumpy guard", ignore="the mailman", language="Polish")
    s = build_system_prompt(p)
    assert "Grumpy guard" in s and "the mailman" in s and "Polish" in s and "threat_level" in s


def test_encode_image_resizes():
    img = np.zeros((1080, 1920, 3), dtype=np.uint8)
    data = encode_image(img, 640, 80)
    assert data.startswith("data:image/jpeg;base64,")
    assert len(data) < 20000


# ---- gate -------------------------------------------------------------------------------------
def test_gate_triggers_on_motion_only():
    gate = FrameGate(sensitivity=0.02, min_interval=0.0)
    still = np.full((240, 320, 3), 100, dtype=np.uint8)
    assert gate.check(Frame("c", still)) is None  # first frame = background
    assert gate.check(Frame("c", still.copy())) is None
    moving = still.copy()
    moving[50:200, 50:200] = 255
    assert gate.check(Frame("c", moving)) == "motion"


def test_gate_passes_explicit_trigger():
    gate = FrameGate(sensitivity=0.5)
    still = np.zeros((240, 320, 3), dtype=np.uint8)
    assert gate.check(Frame("c", still, trigger="person")) == "person"


# ---- policy -----------------------------------------------------------------------------------
def _analysis(threat: int, msg: str = "Go away") -> Analysis:
    return Analysis(threat_level=threat, message=msg)


def test_policy_disarmed_is_quiet():
    p = Policy(PolicyConfig(armed=False))
    assert not p.decide("cam", _analysis(10)).speak


def test_policy_threshold_and_cooldown():
    p = Policy(PolicyConfig(armed=True, threat_threshold=6, cooldown_seconds=100, global_cooldown_seconds=0))
    assert not p.decide("cam", _analysis(5)).speak
    d = p.decide("cam", _analysis(6))
    assert d.speak
    p.mark_spoken("cam")
    assert "cooldown" in p.decide("cam", _analysis(9)).reason
    assert p.decide("other", _analysis(9)).speak  # per-camera cooldown


def test_policy_no_message_means_silent():
    p = Policy(PolicyConfig(armed=True, threat_threshold=1))
    assert not p.decide("cam", _analysis(9, msg="")).speak


def test_schedule_crossing_midnight():
    cfg = PolicyConfig(armed=False, schedule_enabled=True,
                       schedule=[ScheduleWindow(days=[0], start="22:00", end="06:00")])  # Monday night
    p = Policy(cfg)
    assert p.is_armed(datetime(2026, 9, 14, 23, 0))  # Monday 23:00
    assert p.is_armed(datetime(2026, 9, 15, 3, 0))   # Tuesday 03:00 (still Monday's window)
    assert not p.is_armed(datetime(2026, 9, 15, 23, 0))  # Tuesday 23:00 not scheduled
    assert not p.is_armed(datetime(2026, 9, 14, 12, 0))


# ---- eufy / audio -----------------------------------------------------------------------------
def test_decode_buffer_variants():
    assert decode_buffer({"type": "Buffer", "data": [1, 2, 3]}) == b"\x01\x02\x03"
    assert decode_buffer([4, 5]) == b"\x04\x05"
    assert decode_buffer("AQID") == b"\x01\x02\x03"
    assert decode_buffer(None) == b""


def test_wav_to_adts_frames():
    rate = 22050
    pcm = (np.sin(np.arange(rate) / rate * 2 * np.pi * 440) * 10000).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.tobytes())
    frames = wav_to_adts(buf.getvalue())
    assert len(frames) >= 14  # ~1 s at 16 kHz / 1024 samples per frame
    assert all(f[0] == 0xFF and (f[1] & 0xF0) == 0xF0 for f in frames)


# ---- config -----------------------------------------------------------------------------------
def test_config_roundtrip(tmp_path):
    from screaming_camera.config import load_config, save_config
    cfg = AppConfig()
    cfg.prompt.persona = "Test persona"
    path = tmp_path / "c.yaml"
    save_config(cfg, path)
    assert load_config(path).prompt.persona == "Test persona"
