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
import uuid
from collections import defaultdict
from typing import Any, Awaitable, Callable

import websockets

log = logging.getLogger(__name__)

SCHEMA_VERSION = 21
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
                    await self._handshake()
                    async for raw in ws:
                        await self._dispatch(json.loads(raw))
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

    async def _handshake(self) -> None:
        await self.send("set_api_schema", schemaVersion=SCHEMA_VERSION)
        state = await self.send("start_listening")
        st = state.get("state", {})
        self.driver_connected = bool(st.get("driver", {}).get("connected", False))
        for dev in st.get("devices", []):
            if "serialNumber" in dev:
                self.devices[dev["serialNumber"]] = dev
        for station in st.get("stations", []):
            if "serialNumber" in station:
                self.stations[station["serialNumber"]] = station
        log.info("eufy-ws: driver connected=%s, %d devices, %d stations",
                 self.driver_connected, len(self.devices), len(self.stations))
        if not self.driver_connected:
            log.warning("eufy-ws: driver not connected - check credentials / 2FA / captcha in the sidecar logs")

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
            if source == "driver" and name in ("connected", "disconnected"):
                self.driver_connected = name == "connected"
            if source == "device" and name == "property changed" and ev.get("serialNumber") in self.devices:
                self.devices[ev["serialNumber"]][ev.get("name", "")] = ev.get("value")
            for handler in self._handlers.get(f"{source}:{name}", []):
                try:
                    res = handler(ev)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:  # noqa: BLE001
                    log.exception("eufy event handler failed for %s:%s", source, name)
            return
        if mtype == "version":
            log.info("eufy-ws server %s (schema %s-%s)", msg.get("serverVersion"),
                     msg.get("minSchemaVersion"), msg.get("maxSchemaVersion"))

    # ---- commands --------------------------------------------------------------------------
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

    async def start_talkback(self, serial: str) -> None:
        await self.send("device.start_talkback", serialNumber=serial)

    async def talkback_audio_data(self, serial: str, chunk: bytes) -> None:
        # Buffer.from(array) on the Node side; lists are verbose but unambiguous.
        await self.send("device.talkback_audio_data", serialNumber=serial, buffer=list(chunk))

    async def stop_talkback(self, serial: str) -> None:
        try:
            await self.send("device.stop_talkback", serialNumber=serial)
        except Exception as e:  # noqa: BLE001
            log.debug("stop_talkback %s: %s", serial, e)

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
