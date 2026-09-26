"""FastAPI application: JSON API + WebSocket live feed + static control panel."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import AppConfig, ConfigStore, PolicyConfig, config_warnings
from .pipeline import Engine

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


# Request models live at module level: with `from __future__ import annotations` FastAPI resolves the
# string annotations against module globals, so locally defined classes would be treated as query params.
class ArmRequest(BaseModel):
    armed: bool


class SpeakRequest(BaseModel):
    text: str = ""
    speakers: list[str]
    tone: bool = False  # play a 2 s beep instead of TTS - isolates the speaker path from TTS


class CodeRequest(BaseModel):
    code: str


class CameraTestRequest(BaseModel):
    url: str = ""
    username: str = ""
    password: str = ""
    camera_id: str = ""  # test a saved camera instead of ad-hoc values


def create_app(store: ConfigStore) -> FastAPI:
    engine = Engine(store)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await engine.start()
        try:
            yield
        finally:
            await engine.stop()

    app = FastAPI(title="Screaming Camera", lifespan=lifespan)
    app.state.engine = engine

    # ---- panel ------------------------------------------------------------------------------
    # Cache-bust static assets with the file mtime so the panel never shows a stale app.js after an update.
    def _asset_version() -> str:
        return str(int(max((STATIC_DIR / n).stat().st_mtime for n in ("index.html", "app.js", "style.css"))))

    @app.get("/", response_class=HTMLResponse)
    async def index():
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        v = _asset_version()
        html = html.replace('/static/style.css"', f'/static/style.css?v={v}"').replace('/static/app.js"', f'/static/app.js?v={v}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---- state & config ---------------------------------------------------------------------
    @app.get("/api/state")
    async def get_state():
        return engine.state()

    @app.get("/api/warnings")
    async def warnings():
        return {"warnings": config_warnings(engine.cfg)}

    @app.get("/api/config")
    async def get_config():
        return engine.cfg.model_dump(mode="json")

    @app.put("/api/config")
    async def put_config(cfg: AppConfig):
        await engine.apply_config(cfg)
        return {"ok": True, "version": store.version, "warnings": config_warnings(cfg)}

    @app.post("/api/arm")
    async def arm(req: ArmRequest):
        policy = engine.cfg.policy.model_copy(update={"armed": req.armed})
        new_cfg = engine.cfg.model_copy(update={"policy": policy})
        await engine.apply_config(new_cfg)
        return {"armed": engine.policy.is_armed()}

    # ---- events -----------------------------------------------------------------------------
    @app.get("/api/events")
    async def list_events(limit: int = 50, before_id: int | None = None, camera_id: str | None = None,
                          spoken_only: bool = False):
        return await engine.events.list(limit=min(limit, 500), before_id=before_id, camera_id=camera_id,
                                        spoken_only=spoken_only)

    @app.get("/api/stats")
    async def stats():
        return await engine.events.stats()

    @app.get("/api/snapshots/{name}")
    async def snapshot(name: str):
        path = Path(engine.cfg.storage.snapshots_dir) / Path(name).name
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg")

    # ---- cameras ----------------------------------------------------------------------------
    @app.get("/api/cameras/{camera_id}/snapshot.jpg")
    async def camera_snapshot(camera_id: str):
        w = engine.workers.get(camera_id)
        if w is None or w.latest_jpeg is None:
            raise HTTPException(404, "no frame yet")
        return Response(w.latest_jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/cameras/{camera_id}/stream.mjpg")
    async def camera_stream(camera_id: str):
        if camera_id not in engine.workers:
            raise HTTPException(404)

        async def gen():
            last = None
            while True:
                w = engine.workers.get(camera_id)
                if w is None:
                    break
                jpeg = w.latest_jpeg
                if jpeg is not None and jpeg is not last:
                    last = jpeg
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                await asyncio.sleep(0.15)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.post("/api/cameras/{camera_id}/trigger")
    async def camera_trigger(camera_id: str):
        try:
            await engine.trigger_camera(camera_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    # ---- tests ------------------------------------------------------------------------------
    @app.post("/api/test/analyze")
    async def test_analyze(camera_id: str | None = None, act: bool = False, file: UploadFile | None = File(None)):
        image = None
        if file is not None:
            data = np.frombuffer(await file.read(), dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if image is None:
                raise HTTPException(400, "not an image")
        try:
            return await engine.test_analyze(camera_id, image, act=act)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/test/speak")
    async def test_speak(req: SpeakRequest):
        unknown = [s for s in req.speakers if s not in engine.speakers]
        ok = await engine.play_tone(req.speakers) if req.tone else await engine.speak(req.text, req.speakers)
        return {"ok": ok,
                "speakers": {s: {"status": engine.speakers[s].status, "error": engine.speakers[s].last_error}
                             for s in req.speakers if s in engine.speakers},
                "unknown": unknown}

    @app.post("/api/test/camera")
    async def test_camera(req: CameraTestRequest):
        from .cameras.rtsp import build_url, probe
        url, user, pwd = req.url, req.username, req.password
        if req.camera_id:
            cam = engine.cfg.camera(req.camera_id)
            if cam is None:
                raise HTTPException(404, "unknown camera")
            url, user, pwd = cam.url, cam.username, cam.password
        if not url:
            raise HTTPException(400, "no URL")
        result = await asyncio.to_thread(probe, url, user, pwd)
        # never echo the password back to the browser
        result["url"] = build_url(url, user, "***" if pwd else "")
        return result

    @app.get("/api/audio/devices")
    async def audio_devices():
        from .speakers.local_audio import list_output_devices
        try:
            return await asyncio.to_thread(list_output_devices)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    @app.get("/api/model/health")
    async def model_health():
        engine.model_health = await engine.analyzer.health()
        return engine.model_health

    @app.get("/api/eufy/devices")
    async def eufy_devices():
        if not engine.eufy:
            return {"enabled": False, "devices": []}
        return {"enabled": True, **engine.eufy.login_state(),
                "devices": engine.eufy.device_summary(), "stations": list(engine.eufy.stations.keys())}

    def _eufy():
        if not engine.eufy or not engine.eufy.connected:
            raise HTTPException(400, "Eufy bridge not connected - enable it in settings and start docker compose")
        return engine.eufy

    @app.post("/api/eufy/verify_code")
    async def eufy_verify_code(req: CodeRequest):
        try:
            await _eufy().set_verify_code(req.code)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.post("/api/eufy/captcha")
    async def eufy_captcha(req: CodeRequest):
        try:
            await _eufy().set_captcha(req.code)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.post("/api/eufy/connect")
    async def eufy_connect():
        try:
            await _eufy().connect_driver()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.post("/api/eufy/refresh")
    async def eufy_refresh():
        try:
            await _eufy().refresh_state()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        return {"ok": True, "devices": engine.eufy.device_summary()}

    # ---- live feed --------------------------------------------------------------------------
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        q = engine.bus.subscribe()
        try:
            await websocket.send_json({"type": "state", "data": engine.state()})
            while True:
                msg = await q.get()
                await websocket.send_json(msg)
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001
            log.debug("ws closed: %s", e)
        finally:
            engine.bus.unsubscribe(q)

    return app
