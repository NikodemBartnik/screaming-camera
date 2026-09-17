"""Engine: wires cameras -> gate -> analyzer -> policy -> TTS -> speakers, and exposes live state
to the web layer. One Engine per process; one CameraWorker per enabled camera."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import cv2
import numpy as np

from .analyzer import Analysis, Analyzer
from .cameras import CameraSource, Frame, create_source
from .config import AppConfig, CameraConfig, ConfigStore
from .eufy.ws_client import EufyWsClient
from .gate import FrameGate
from .policy import Policy
from .speakers import Speaker, create_speaker
from .store import EventStore
from .tts import TTS

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self):
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, kind: str, data: Any) -> None:
        msg = {"type": kind, "data": data, "ts": time.time()}
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)


class CameraWorker:
    def __init__(self, engine: "Engine", cfg: CameraConfig):
        self.engine = engine
        self.cfg = cfg
        self.queue: asyncio.Queue[Frame] = asyncio.Queue(maxsize=4)
        self.source: CameraSource = create_source(cfg, self.queue, engine.eufy)
        self.gate = FrameGate(cfg.motion_sensitivity)
        self.latest_jpeg: bytes | None = None
        self.latest_frame: Frame | None = None
        self.frames_seen = 0
        self.last_frame_ts = 0.0
        self.last_trigger_ts = 0.0
        self.busy = False
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self.source.run(), name=f"src-{self.cfg.id}"),
            asyncio.create_task(self._consume(), name=f"consume-{self.cfg.id}"),
        ]

    async def stop(self) -> None:
        self.source.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _consume(self) -> None:
        while True:
            frame = await self.queue.get()
            self.frames_seen += 1
            self.last_frame_ts = frame.ts
            self.latest_frame = frame
            self.latest_jpeg = await asyncio.to_thread(self._to_jpeg, frame.image)
            trigger = await asyncio.to_thread(self.gate.check, frame)
            if trigger and trigger != "manual" and not self.engine.policy.is_armed() \
                    and not self.engine.cfg.policy.analyze_when_disarmed:
                continue  # user chose to save compute while disarmed
            if trigger and not self.busy:
                self.busy = True
                self.last_trigger_ts = frame.ts
                frames = self.gate.recent_frames(self.engine.cfg.model.frames_per_request)
                asyncio.create_task(self._analyze(frames, trigger), name=f"analyze-{self.cfg.id}")

    async def _analyze(self, frames: list[Frame], trigger: str) -> None:
        try:
            await self.engine.analyze_and_act(self.cfg, frames, trigger)
        except Exception:  # noqa: BLE001
            log.exception("camera %s: analysis failed", self.cfg.id)
        finally:
            self.busy = False

    @staticmethod
    def _to_jpeg(img: np.ndarray) -> bytes:
        h, w = img.shape[:2]
        if w > 960:
            img = cv2.resize(img, (960, int(h * 960 / w)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
        return buf.tobytes() if ok else b""

    def state(self) -> dict[str, Any]:
        return {
            "id": self.cfg.id, "name": self.cfg.name or self.cfg.id, "type": self.cfg.type,
            "status": self.source.status, "error": self.source.last_error,
            "frames": self.frames_seen, "last_frame_age": round(time.time() - self.last_frame_ts, 1) if self.last_frame_ts else None,
            "motion": round(self.gate.last_motion, 4), "busy": self.busy,
            "speakers": self.cfg.speakers,
        }


class Engine:
    def __init__(self, store: ConfigStore):
        self.config_store = store
        self.bus = EventBus()
        self.events = EventStore(self.cfg.storage)
        self.analyzer = Analyzer(self.cfg.model, self.cfg.prompt)
        self.policy = Policy(self.cfg.policy)
        self.tts = TTS(self.cfg.tts)
        self.eufy: EufyWsClient | None = None
        self.speakers: dict[str, Speaker] = {}
        self.workers: dict[str, CameraWorker] = {}
        self._model_lock = asyncio.Lock()
        self._bg: list[asyncio.Task] = []
        self.model_health: dict[str, Any] = {"ok": False}
        self.started_at = time.time()

    @property
    def cfg(self) -> AppConfig:
        return self.config_store.cfg

    # ---- lifecycle -------------------------------------------------------------------------
    async def start(self) -> None:
        await self.events.open()
        await self._start_io()
        self._bg = [asyncio.create_task(self._housekeeping(), name="housekeeping")]
        log.info("engine started with %d cameras, %d speakers", len(self.workers), len(self.speakers))

    async def stop(self) -> None:
        for t in self._bg:
            t.cancel()
        await self._stop_io()
        await self.events.close()

    async def _start_io(self) -> None:
        cfg = self.cfg
        if cfg.eufy.enabled:
            self.eufy = EufyWsClient(cfg.eufy.ws_url)
            self.eufy.start()
        for s in cfg.speakers:
            if not s.enabled:
                continue
            try:
                self.speakers[s.id] = create_speaker(s, self.eufy)
            except Exception as e:  # noqa: BLE001
                log.error("speaker %s: %s", s.id, e)
        for c in cfg.cameras:
            if not c.enabled:
                continue
            try:
                w = CameraWorker(self, c)
                w.start()
                self.workers[c.id] = w
            except Exception as e:  # noqa: BLE001
                log.error("camera %s: %s", c.id, e)

    async def _stop_io(self) -> None:
        for w in list(self.workers.values()):
            await w.stop()
        self.workers.clear()
        for s in list(self.speakers.values()):
            await s.close()
        self.speakers.clear()
        if self.eufy:
            await self.eufy.stop()
            self.eufy = None

    async def apply_config(self, new_cfg: AppConfig) -> None:
        """Persist and hot-apply. Prompt/policy/model/tts update in place; cameras/speakers/eufy restart."""
        old = self.cfg
        self.config_store.update(new_cfg)
        self.analyzer.update(new_cfg.model, new_cfg.prompt)
        self.policy.update(new_cfg.policy)
        self.tts.update(new_cfg.tts)
        io_changed = (old.cameras, old.speakers, old.eufy) != (new_cfg.cameras, new_cfg.speakers, new_cfg.eufy)
        if io_changed:
            await self._stop_io()
            await self._start_io()
        self.bus.publish("config", {"version": self.config_store.version, "io_restarted": io_changed})
        self.bus.publish("state", self.state())

    async def _housekeeping(self) -> None:
        last_cleanup = 0.0
        while True:
            self.model_health = await self.analyzer.health()
            if time.time() - last_cleanup > 3600:
                try:
                    n = await self.events.cleanup()
                    if n:
                        log.info("cleaned up %d old events", n)
                except Exception:  # noqa: BLE001
                    log.exception("cleanup failed")
                last_cleanup = time.time()
            self.bus.publish("state", self.state())
            await asyncio.sleep(15)

    # ---- core ------------------------------------------------------------------------------
    async def analyze_and_act(self, cam: CameraConfig, frames: list[Frame], trigger: str,
                              act: bool = True, context: str = "") -> dict[str, Any]:
        t0 = time.time()
        self.bus.publish("analyzing", {"camera_id": cam.id, "trigger": trigger})
        async with self._model_lock:
            analysis = await self.analyzer.analyze([f.image for f in frames], cam.name or cam.id, trigger, context)
        armed = self.policy.is_armed()
        decision = self.policy.decide(cam.id, analysis) if act else None
        spoken = False
        if decision and decision.speak:
            self.policy.mark_spoken(cam.id)
            spoken = await self.speak(analysis.message, cam.speakers)
        snapshot = await asyncio.to_thread(self.events.save_snapshot, frames[-1].image, cam.id, frames[-1].ts)
        event = await self.events.add(
            ts=t0, camera_id=cam.id, camera_name=cam.name or cam.id, trigger=trigger, analysis=analysis,
            spoken=spoken, decision=(decision.reason if decision else "test only"), speakers=cam.speakers,
            snapshot=snapshot, armed=armed,
        )
        event["total_ms"] = int((time.time() - t0) * 1000)
        self.bus.publish("event", event)
        log.info("[%s] %s threat=%d spoken=%s (%s) %dms: %s", cam.id, trigger, analysis.threat_level, spoken,
                 event["decision"], analysis.latency_ms, analysis.scene)
        return event

    async def speak(self, text: str, speaker_ids: list[str]) -> bool:
        """Synthesize once, play on all listed speakers concurrently. Returns True if any speaker played."""
        targets = [self.speakers[s] for s in speaker_ids if s in self.speakers]
        if not targets:
            log.warning("no active speakers among %s", speaker_ids)
            return False
        try:
            wav = await self.tts.synthesize(text)
        except Exception as e:  # noqa: BLE001
            log.error("TTS failed: %s", e)
            return False
        self.bus.publish("speaking", {"text": text, "speakers": [t.cfg.id for t in targets]})
        results = await asyncio.gather(*(t.play(wav) for t in targets), return_exceptions=True)
        ok = False
        for t, r in zip(targets, results):
            if isinstance(r, Exception):
                log.error("speaker %s failed: %s", t.cfg.id, r)
            else:
                ok = True
        return ok

    async def test_analyze(self, camera_id: str | None, image: np.ndarray | None, act: bool = False) -> dict[str, Any]:
        if image is not None:
            cam = self.cfg.camera(camera_id) if camera_id else None
            cam = cam or CameraConfig(id="upload", name="Uploaded image", type="file")
            frames = [Frame(cam.id, image)]
        else:
            w = self.workers.get(camera_id or "")
            if w is None or w.latest_frame is None:
                raise ValueError("camera has no frame yet")
            cam = w.cfg
            frames = [w.latest_frame]
        return await self.analyze_and_act(cam, frames, "manual test", act=act)

    async def trigger_camera(self, camera_id: str) -> None:
        w = self.workers.get(camera_id)
        if w is None:
            raise ValueError("unknown camera")
        src = w.source
        if hasattr(src, "trigger"):
            await src.trigger("manual")  # eufy: wake the livestream
        elif w.latest_frame is not None:
            w.latest_frame.trigger = "manual"
            w.queue.put_nowait(w.latest_frame)

    # ---- state -----------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        return {
            "armed": self.policy.is_armed(),
            "armed_manual": self.cfg.policy.armed,
            "schedule_enabled": self.cfg.policy.schedule_enabled,
            "cameras": [w.state() for w in self.workers.values()],
            "speakers": [{"id": s.cfg.id, "name": s.cfg.name or s.cfg.id, "type": s.cfg.type,
                          "status": s.status, "error": s.last_error} for s in self.speakers.values()],
            "model": {"endpoint": self.cfg.model.endpoint, "name": self.cfg.model.name,
                      "status": self.analyzer.status, "error": self.analyzer.last_error, **self.model_health},
            "tts": {"engine": self.cfg.tts.engine, "status": self.tts.status, "error": self.tts.last_error},
            "eufy": {"enabled": self.cfg.eufy.enabled, "connected": bool(self.eufy and self.eufy.connected),
                     "driver_connected": bool(self.eufy and self.eufy.driver_connected),
                     "error": self.eufy.last_error if self.eufy else "",
                     "devices": self.eufy.device_summary() if self.eufy else []},
            "uptime": int(time.time() - self.started_at),
            "config_version": self.config_store.version,
        }
