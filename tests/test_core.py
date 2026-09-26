"""Hardware-free tests for the parts that have logic worth guarding."""
from __future__ import annotations

import io
import time
import wave
from datetime import datetime

import numpy as np
import pytest

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


# ---- rtsp url building -------------------------------------------------------------------------
def test_build_url_encodes_credentials():
    from screaming_camera.cameras.rtsp import build_url
    assert build_url("rtsp://192.168.1.5:554/stream2", "cam", "p@ss:word/1") == \
        "rtsp://cam:p%40ss%3Aword%2F1@192.168.1.5:554/stream2"
    # credentials already in the URL are left alone
    assert build_url("rtsp://a:b@1.2.3.4/live0", "cam", "x") == "rtsp://a:b@1.2.3.4/live0"
    # no credentials configured -> untouched
    assert build_url("rtsp://1.2.3.4/live0") == "rtsp://1.2.3.4/live0"
    # bare host gets a scheme
    assert build_url("1.2.3.4:554/stream1", "u", "p") == "rtsp://u:p@1.2.3.4:554/stream1"


def test_explain_error_is_actionable():
    from screaming_camera.cameras.rtsp import explain_error
    assert "camera account" in explain_error(Exception("Server returned 401 Unauthorized")).lower()
    assert "404" in explain_error(Exception("Server returned 404 Not Found"))
    assert "no response" in explain_error(Exception("Immediate exit requested")).lower()


# ---- tapo talkback (protocol built from go2rtc's implementation) --------------------------------
def test_alaw_matches_reference():
    """Our vectorised A-law encoder must agree with the stdlib implementation byte for byte."""
    audioop = pytest.importorskip("audioop")
    from screaming_camera.speakers.tapo_talkback import linear_to_alaw
    rng = np.random.default_rng(0)
    pcm = np.concatenate([rng.integers(-32768, 32767, 5000, dtype=np.int64).astype(np.int16),
                          np.array([-32768, -1, 0, 1, 32767], dtype=np.int16)])
    assert linear_to_alaw(pcm) == audioop.lin2alaw(pcm.tobytes(), 2)


def test_mpegts_header_and_payload_structure():
    from screaming_camera.speakers.tapo_talkback import (PCMA_STREAM_TYPE, TS_PACKET, _crc32_mpeg,
                                                         ts_header, ts_payload)
    head = ts_header()
    assert len(head) == 2 * TS_PACKET
    pat, pmt = head[:TS_PACKET], head[TS_PACKET:]
    for pkt in (pat, pmt):
        assert pkt[0] == 0x47 and pkt[1] & 0x40  # sync + payload unit start
        # 4-byte TS header, then pointer field, then the PSI section itself
        section_len = ((pkt[6] & 0x0F) << 8) | pkt[7]
        section = pkt[5:5 + 3 + section_len]      # table id .. CRC inclusive
        assert _crc32_mpeg(section) == 0, "PSI CRC must verify to zero over section+CRC"
    assert PCMA_STREAM_TYPE in pmt, "PMT must announce the Tapo A-law stream type"

    body, counter = ts_payload(b"\xd5" * 480, 0, 0)
    assert len(body) % TS_PACKET == 0 and counter == len(body) // TS_PACKET
    assert all(body[i] == 0x47 for i in range(0, len(body), TS_PACKET))
    assert body[1] & 0x40, "first TS packet carries the PES start indicator"
    assert body[4:8] == b"\x00\x00\x01\xc0", "PES start code + audio stream id"


def test_wav_to_pcma_resamples_to_8k():
    from screaming_camera.speakers.tapo_talkback import RATE, wav_to_pcma
    rate = 22050
    pcm = (np.sin(np.arange(rate) / rate * 2 * np.pi * 440) * 12000).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.tobytes())
    alaw = wav_to_pcma(buf.getvalue())
    assert abs(len(alaw) - RATE) < RATE * 0.05  # ~1 s of 8 kHz A-law, one byte per sample


def test_config_warnings_catch_silent_misconfiguration():
    from screaming_camera.config import CameraConfig, SpeakerConfig, config_warnings
    cfg = AppConfig(
        speakers=[SpeakerConfig(id="tapo_cam", name="Tapo", type="tapo_talkback", host="1.2.3.4", password=""),
                  SpeakerConfig(id="off", type="local_audio", enabled=False)],
        cameras=[CameraConfig(id="a", type="rtsp", url="rtsp://x/y", speakers=["tapo_cam"]),
                 CameraConfig(id="b", type="rtsp", url="", speakers=["off"]),
                 CameraConfig(id="c", type="eufy_p2p", serial="T1", speakers=[])],
    )
    warnings = " | ".join(config_warnings(cfg))
    assert "cloud password" in warnings          # enabled Tapo speaker without a password
    assert "no RTSP URL" in warnings             # camera b
    assert "disabled or gone" in warnings        # camera b points at a disabled speaker
    assert "no speaker assigned" in warnings     # camera c
    assert "Speaker 'off'" not in warnings       # disabled speakers are not nagged about


def test_config_warnings_silent_when_complete():
    from screaming_camera.config import CameraConfig, SpeakerConfig, config_warnings
    cfg = AppConfig(
        speakers=[SpeakerConfig(id="bt", type="local_audio")],
        cameras=[CameraConfig(id="a", type="rtsp", url="rtsp://x/y", speakers=["bt"])],
    )
    assert config_warnings(cfg) == []
