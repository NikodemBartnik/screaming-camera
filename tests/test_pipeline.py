"""Integration test of the engine with a fake model and a fake speaker (no network, no audio)."""
from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path

import numpy as np
import pytest

from screaming_camera.analyzer import Analysis, Person
from screaming_camera.cameras.base import Frame
from screaming_camera.config import AppConfig, CameraConfig, ConfigStore, SpeakerConfig
from screaming_camera.pipeline import Engine
from screaming_camera.speakers.base import Speaker


class FakeSpeaker(Speaker):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.played: list[bytes] = []

    async def play(self, wav: bytes) -> None:
        self.played.append(wav)


class FakeAnalyzer:
    status = "ok"
    last_error = ""

    def __init__(self, verdict: Analysis):
        self.verdict = verdict
        self.calls = 0

    def update(self, *_):
        pass

    async def health(self):
        return {"ok": True}

    async def analyze(self, images, camera_name, trigger, context=""):
        self.calls += 1
        return self.verdict


class FakeTTS:
    status = "ok"
    last_error = ""

    def update(self, *_):
        pass

    async def synthesize(self, text: str) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(np.zeros(1600, dtype=np.int16).tobytes())
        return buf.getvalue()


@pytest.fixture
async def engine(tmp_path: Path):
    cfg = AppConfig()
    cfg.storage.db_path = str(tmp_path / "e.sqlite")
    cfg.storage.snapshots_dir = str(tmp_path / "snaps")
    cfg.cameras = [CameraConfig(id="cam", name="Front", type="file", path=str(tmp_path), enabled=False, speakers=["spk"])]
    cfg.speakers = [SpeakerConfig(id="spk", type="local_audio", enabled=False)]
    store = ConfigStore(tmp_path / "config.yaml")
    store.update(cfg)
    eng = Engine(store)
    eng.tts = FakeTTS()
    await eng.events.open()
    eng.speakers["spk"] = FakeSpeaker(cfg.speakers[0])
    yield eng
    await eng.events.close()


def _frame() -> Frame:
    return Frame("cam", np.zeros((120, 160, 3), dtype=np.uint8))


async def test_high_threat_speaks_when_armed(engine: Engine):
    engine.analyzer = FakeAnalyzer(Analysis(threat_level=9, scene="man at gate", message="Hey you, in the hoodie!",
                                            people=[Person(clothing="black hoodie", action="pulling the gate")]))
    engine.policy.update(engine.cfg.policy.model_copy(update={"armed": True}))
    q = engine.bus.subscribe()
    ev = await engine.analyze_and_act(engine.cfg.cameras[0], [_frame()], "motion")
    assert ev["spoken"] is True and ev["threat_level"] == 9 and ev["people"][0]["clothing"] == "black hoodie"
    assert engine.speakers["spk"].played, "speaker should have received audio"
    assert (Path(engine.cfg.storage.snapshots_dir) / ev["snapshot"]).exists()
    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait()["type"])
    assert "analyzing" in kinds and "speaking" in kinds and "event" in kinds
    # second event right away -> camera cooldown
    ev2 = await engine.analyze_and_act(engine.cfg.cameras[0], [_frame()], "motion")
    assert ev2["spoken"] is False and "cooldown" in ev2["decision"]


async def test_disarmed_logs_but_stays_quiet(engine: Engine):
    engine.analyzer = FakeAnalyzer(Analysis(threat_level=9, scene="x", message="Go away"))
    ev = await engine.analyze_and_act(engine.cfg.cameras[0], [_frame()], "person")
    assert ev["spoken"] is False and ev["decision"] == "disarmed"
    assert not engine.speakers["spk"].played
    rows = await engine.events.list()
    assert len(rows) == 1 and rows[0]["message"] == "Go away"


async def test_test_analyze_never_speaks(engine: Engine):
    engine.analyzer = FakeAnalyzer(Analysis(threat_level=10, message="Boo"))
    engine.policy.update(engine.cfg.policy.model_copy(update={"armed": True}))
    ev = await engine.test_analyze(None, np.zeros((50, 50, 3), dtype=np.uint8), act=False)
    assert ev["spoken"] is False and ev["decision"] == "test only"
