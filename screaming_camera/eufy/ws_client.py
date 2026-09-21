"""Async client for bropat/eufy-security-ws (Node.js sidecar, see docker-compose.yml).

Protocol: JSON over WebSocket.
  -> {"messageId": "...", "command": "device.start_livestream", "serialNumber": "..."}
  <- {"type": "result", "messageId": "...", "success": true, "result": {...}}
  <- {"type": "event", "event": {"source": "device", "event": "motion detected", "serialNumber": "...", "state": true}}
Binary buffers arrive JSON-serialised as {"type": "Buffer", "data": [ints]} (or base64 in some versions).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from collections import defaultdict
from typing import Any, Awaitable, Callable

import websockets

log = logging.getLogger(__name__)

SCHEMA_VERSION = 21
GUARD_MODES = {"away": 0, "home": 1, "schedule": 2, "custom1": 3, "custom2": 4, "custom3": 5, "off": 6,
               "geo": 47, "disarmed": 63}
EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


def decode_buffer(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, dict) and "data" in value:
        return bytes(value["data"])
    if isinstance(value, list):
        return bytes(value)
    if isinstance(value, str):
        return base64.b64decode(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return b""


class EufyWsClient:
    def __init__(self, url: str):
        self.url = url
        self.connected = False
        self.driver_connected = False
        self.last_error = ""
        self.devices: dict[str, dict[str, Any]] = {}  # serial -> properties
        self.stations: dict[str, dict[str, Any]] = {}
        # first-login helpers: Eufy asks for an e-mail 2FA code and sometimes a captcha
        self.needs_verify_code = False
        self.captcha_id: str | None = None
        self.captcha_image: str | None = None  # data URL / base64 png
        self.connection_error = ""
        self.livestreams: set[str] = set()  # serials with a running livestream (from events)
        self.talkbacks: set[str] = set()  # serials with a station-confirmed talkback session
        self.stream_holds: dict[str, float] = {}  # serial -> monotonic deadline; keeps a stream open (talkback)
        self._ws: websockets.ClientConnection | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)  # key: "device:motion detected"
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ---- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="eufy-ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    def on(self, source: str, event: str, handler: EventHandler) -> None:
        self._handlers[f"{source}:{event}"].append(handler)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, max_size=None, ping_interval=20) as ws:
                    self._ws = ws
                    self.connected = True
                    self.last_error = ""
                    log.info("eufy-ws connected to %s", self.url)
                    # The handshake awaits command results, which only the read loop below can deliver,
                    # so it must run concurrently with it - never inline before it.
                    handshake = asyncio.create_task(self._handshake(), name="eufy-handshake")
                    handshake.add_done_callback(self._log_task_failure)
                    try:
                        async for raw in ws:
                            await self._dispatch(json.loads(raw))
                    finally:
                        handshake.cancel()
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                log.warning("eufy-ws: %s (retry in 5s)", e)
            finally:
                self.connected = False
                self.driver_connected = False
                self._ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("eufy-ws disconnected"))
                self._pending.clear()
            if not self._stop.is_set():
                await asyncio.sleep(5)

    @staticmethod
    def _log_task_failure(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        if exc := task.exception():
            log.warning("eufy-ws: %s failed: %s", task.get_name(), exc)

    def _spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=f"eufy-{name}")
        task.add_done_callback(self._log_task_failure)
        return task

    async def _handshake(self) -> None:
        try:
            await self.send("set_api_schema", schemaVersion=SCHEMA_VERSION)
            await self.refresh_state()
        except Exception as e:  # noqa: BLE001 - drop the socket so the run loop reconnects cleanly
            self.last_error = f"handshake: {e}"
            log.warning("eufy-ws: handshake failed (%s), reconnecting", e)
            if self._ws is not None:
                await self._ws.close()

    async def refresh_state(self) -> None:
        state = await self.send("start_listening")
        st = state.get("state", {})
        self.driver_connected = bool(st.get("driver", {}).get("connected", False))
        # Schema >= 13 lists devices/stations as serial-number strings; older schemas as full objects.
        await asyncio.gather(*(self._load_device(d) for d in st.get("devices", [])))
        await asyncio.gather(*(self._load_station(s) for s in st.get("stations", [])))
        log.info("eufy-ws: driver connected=%s, %d devices, %d stations",
                 self.driver_connected, len(self.devices), len(self.stations))
        for serial, props in self.devices.items():
            log.info("eufy-ws: device %s = %s (%s) on station %s", serial, props.get("name"),
                     props.get("model"), props.get("stationSerialNumber"))
        if not self.driver_connected:
            log.warning("eufy-ws: driver not connected - check credentials / 2FA / captcha in the sidecar logs")

    async def _load_device(self, item: Any) -> None:
        if isinstance(item, dict) and "serialNumber" in item:
            self.devices[item["serialNumber"]] = item
            return
        serial = str(item)
        try:
            res = await self.send("device.get_properties", serialNumber=serial)
            self.devices[serial] = {"serialNumber": serial, **(res.get("properties") or {})}
        except Exception as e:  # noqa: BLE001
            log.warning("eufy-ws: get_properties for device %s failed: %s", serial, e)
            self.devices.setdefault(serial, {"serialNumber": serial})

    async def _load_station(self, item: Any) -> None:
        if isinstance(item, dict) and "serialNumber" in item:
            self.stations[item["serialNumber"]] = item
            return
        serial = str(item)
        try:
            res = await self.send("station.get_properties", serialNumber=serial)
            self.stations[serial] = {"serialNumber": serial, **(res.get("properties") or {})}
        except Exception as e:  # noqa: BLE001
            log.warning("eufy-ws: get_properties for station %s failed: %s", serial, e)
            self.stations.setdefault(serial, {"serialNumber": serial})

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "result":
            fut = self._pending.pop(msg.get("messageId", ""), None)
            if fut is not None and not fut.done():
                if msg.get("success"):
                    fut.set_result(msg.get("result", {}))
                else:
                    fut.set_exception(RuntimeError(f"{msg.get('errorCode')}: {msg.get('errorMessage', '')}"))
            return
        if mtype == "event":
            ev = msg.get("event", {})
            source, name = ev.get("source"), ev.get("event")
            if name not in ("livestream video data", "livestream audio data"):
                extra = {k: v for k, v in ev.items() if k not in ("source", "event", "captcha")}
                log.info("eufy event %s:%s %s", source, name, json.dumps(extra, ensure_ascii=False)[:200])
            if source == "driver":
                self._spawn(self._on_driver_event(name, ev), f"driver:{name}")
            if source == "device" and name == "property changed" and ev.get("serialNumber") in self.devices:
                self.devices[ev["serialNumber"]][ev.get("name", "")] = ev.get("value")
            elif source == "device" and name == "livestream started":
                self.livestreams.add(str(ev.get("serialNumber")))
            elif source == "device" and name == "livestream stopped":
                self.livestreams.discard(str(ev.get("serialNumber")))
                self.talkbacks.discard(str(ev.get("serialNumber")))
            elif source == "device" and name == "talkback started":
                self.talkbacks.add(str(ev.get("serialNumber")))
            elif source == "device" and name == "talkback stopped":
                self.talkbacks.discard(str(ev.get("serialNumber")))
            elif source == "device" and name == "device added":
                self._spawn(self._load_device(ev.get("device")), "device-added")
            elif source == "device" and name == "device removed":
                self.devices.pop(str(ev.get("device")), None)
            elif source == "station" and name == "station added":
                self._spawn(self._load_station(ev.get("station")), "station-added")
            elif source == "station" and name == "station removed":
                self.stations.pop(str(ev.get("station")), None)
            for handler in self._handlers.get(f"{source}:{name}", []):
                try:
                    res = handler(ev)
                    if asyncio.iscoroutine(res):
                        # Async handlers may send commands and await their results, which this very
                        # loop delivers - run them as tasks so the read loop never blocks on them.
                        self._spawn(res, f"{source}:{name}")
                except Exception:  # noqa: BLE001
                    log.exception("eufy event handler failed for %s:%s", source, name)
            return
        if mtype == "version":
            log.info("eufy-ws server %s (schema %s-%s)", msg.get("serverVersion"),
                     msg.get("minSchemaVersion"), msg.get("maxSchemaVersion"))

    async def _on_driver_event(self, name: str, ev: dict[str, Any]) -> None:
        if name == "connected":
            self.driver_connected = True
            self.needs_verify_code = False
            self.captcha_id = self.captcha_image = None
            self.connection_error = ""
            log.info("eufy-ws: driver connected to Eufy cloud, refreshing device list")
            try:
                await self.refresh_state()
            except Exception as e:  # noqa: BLE001
                log.warning("eufy-ws: refresh after connect failed: %s", e)
        elif name == "disconnected":
            self.driver_connected = False
        elif name == "verify code":
            self.needs_verify_code = True
            log.warning("eufy-ws: Eufy sent a 2FA code to your e-mail - enter it in the panel (Settings -> Eufy bridge)")
        elif name == "captcha request":
            self.captcha_id = ev.get("captchaId")
            self.captcha_image = ev.get("captcha")
            log.warning("eufy-ws: captcha required - solve it in the panel (Settings -> Eufy bridge)")
        elif name == "connection error":
            self.connection_error = str(ev.get("error", ""))
            log.warning("eufy-ws: connection error: %s", self.connection_error)

    # ---- commands --------------------------------------------------------------------------
    async def set_verify_code(self, code: str) -> None:
        await self.send("driver.set_verify_code", verifyCode=code.strip())
        self.needs_verify_code = False

    async def set_captcha(self, code: str) -> None:
        if not self.captcha_id:
            raise RuntimeError("no captcha pending")
        await self.send("driver.set_captcha", captchaId=self.captcha_id, captcha=code.strip())
        self.captcha_id = self.captcha_image = None

    async def set_guard_mode(self, station_serial: str, mode: str | int) -> None:
        value = GUARD_MODES[mode.lower()] if isinstance(mode, str) else int(mode)
        await self.send("station.set_guard_mode", serialNumber=station_serial, mode=value)

    async def connect_driver(self) -> None:
        await self.send("driver.connect", timeout=30)

    async def send(self, command: str, timeout: float = 15.0, **kwargs: Any) -> dict[str, Any]:
        if self._ws is None:
            raise ConnectionError("eufy-ws not connected")
        message_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = fut
        await self._ws.send(json.dumps({"messageId": message_id, "command": command, **kwargs}))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(message_id, None)

    async def start_livestream(self, serial: str) -> None:
        await self.send("device.start_livestream", serialNumber=serial)

    async def stop_livestream(self, serial: str) -> None:
        try:
            await self.send("device.stop_livestream", serialNumber=serial)
        except Exception as e:  # noqa: BLE001 - already stopped is fine
            log.debug("stop_livestream %s: %s", serial, e)

    async def is_livestreaming(self, serial: str) -> bool:
        res = await self.send("device.is_livestreaming", serialNumber=serial)
        return bool(res.get("livestreaming"))

    def hold_stream(self, serial: str, seconds: float) -> None:
        """Ask camera sources not to stop this livestream for a while (e.g. while talkback plays)."""
        self.stream_holds[serial] = max(self.stream_holds.get(serial, 0.0), time.monotonic() + seconds)

    def stream_held(self, serial: str) -> bool:
        return self.stream_holds.get(serial, 0.0) > time.monotonic()

    async def ensure_livestream(self, serial: str, timeout: float = 12.0) -> bool:
        """Start the livestream if needed and wait until it is running. Returns True if we started it."""
        if serial in self.livestreams or await self.is_livestreaming(serial):
            self.livestreams.add(serial)
            return False
        await self.start_livestream(serial)
        for _ in range(int(timeout / 0.25)):
            await asyncio.sleep(0.25)
            if serial in self.livestreams:
                return True
        raise TimeoutError(f"livestream for {serial} did not start within {timeout}s")

    async def start_talkback(self, serial: str, timeout: float = 10.0) -> None:
        """Open a talkback session and wait until the station confirms it ("talkback started")."""
        await self.send("device.start_talkback", serialNumber=serial)
        for _ in range(int(timeout / 0.1)):
            if serial in self.talkbacks:
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(f"talkback for {serial} not confirmed by the station within {timeout}s")

    async def talkback_audio_data(self, serial: str, chunk: bytes) -> None:
        # Buffer.from(array) on the Node side; lists are verbose but unambiguous.
        await self.send("device.talkback_audio_data", serialNumber=serial, buffer=list(chunk))

    async def stop_talkback(self, serial: str) -> None:
        try:
            await self.send("device.stop_talkback", serialNumber=serial)
        except Exception as e:  # noqa: BLE001
            log.debug("stop_talkback %s: %s", serial, e)

    def login_state(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "driver_connected": self.driver_connected,
            "needs_verify_code": self.needs_verify_code,
            "captcha_id": self.captcha_id,
            "captcha_image": self.captcha_image,
            "connection_error": self.connection_error,
            "error": self.last_error,
        }

    def device_summary(self) -> list[dict[str, Any]]:
        out = []
        for serial, props in self.devices.items():
            out.append({
                "serial": serial,
                "name": props.get("name", ""),
                "model": props.get("model", ""),
                "type": props.get("type", ""),
                "station": props.get("stationSerialNumber", ""),
                "battery": props.get("battery"),
                "motion": props.get("motionDetected"),
            })
        return out
