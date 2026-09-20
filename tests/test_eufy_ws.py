"""EufyWsClient against a fake eufy-security-ws server speaking the real message envelope.
Guards the handshake / read-loop concurrency (a deadlock here showed up only on real hardware)."""
from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from screaming_camera.eufy.ws_client import EufyWsClient

# Schema >= 13: start_listening lists serials only; properties come from *.get_properties.
DEVICE_PROPS = {"T8160TEST": {"name": "Front", "model": "T8160", "type": 19, "stationSerialNumber": "T8030TEST", "battery": 80}}
STATION_PROPS = {"T8030TEST": {"name": "HomeBase 3", "model": "T8030", "lanIpAddress": "192.168.1.20"}}


class FakeEufyWs:
    def __init__(self):
        self.commands: list[dict] = []
        self.clients: list = []
        self.driver_connected = True

    async def handler(self, ws):
        self.clients.append(ws)
        await ws.send(json.dumps({"type": "version", "serverVersion": "test", "minSchemaVersion": 0, "maxSchemaVersion": 21}))
        async for raw in ws:
            msg = json.loads(raw)
            self.commands.append(msg)
            cmd, mid = msg["command"], msg["messageId"]
            if cmd == "set_api_schema":
                result = {}
            elif cmd == "start_listening":
                result = {"state": {"driver": {"connected": self.driver_connected},
                                    "devices": list(DEVICE_PROPS) if self.driver_connected else [],
                                    "stations": list(STATION_PROPS) if self.driver_connected else []}}
            elif cmd == "device.get_properties":
                result = {"serialNumber": msg["serialNumber"], "properties": DEVICE_PROPS[msg["serialNumber"]]}
            elif cmd == "station.get_properties":
                result = {"serialNumber": msg["serialNumber"], "properties": STATION_PROPS[msg["serialNumber"]]}
            elif cmd == "device.start_livestream":
                result = {}
                await ws.send(json.dumps({"type": "event", "event": {"source": "device", "event": "livestream started",
                                                                      "serialNumber": msg["serialNumber"]}}))
            elif cmd == "device.start_talkback":
                result = {}
                await ws.send(json.dumps({"type": "event", "event": {"source": "device", "event": "talkback started",
                                                                      "serialNumber": msg["serialNumber"]}}))
            elif cmd == "driver.set_verify_code":
                self.driver_connected = True
                result = {"result": True}
                await ws.send(json.dumps({"type": "event", "event": {"source": "driver", "event": "connected"}}))
            else:
                result = {}
            await ws.send(json.dumps({"type": "result", "messageId": mid, "success": True, "result": result}))

    async def emit(self, event: dict):
        for ws in self.clients:
            await ws.send(json.dumps({"type": "event", "event": event}))


@pytest.fixture
async def server():
    fake = FakeEufyWs()
    async with websockets.serve(fake.handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        fake.url = f"ws://127.0.0.1:{port}"
        yield fake


async def _wait(cond, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_handshake_loads_devices(server):
    client = EufyWsClient(server.url)
    client.start()
    try:
        assert await _wait(lambda: client.driver_connected and "T8160TEST" in client.devices), \
            f"handshake did not complete; commands seen: {[c['command'] for c in server.commands]}"
        assert [c["command"] for c in server.commands[:2]] == ["set_api_schema", "start_listening"]
        summary = client.device_summary()[0]
        assert summary["station"] == "T8030TEST" and summary["name"] == "Front" and summary["battery"] == 80
        assert client.stations["T8030TEST"]["lanIpAddress"] == "192.168.1.20"
    finally:
        await client.stop()


async def test_event_handler_can_send_commands(server):
    """A motion event handler starts the livestream (command + await result) without deadlocking the reader."""
    client = EufyWsClient(server.url)
    started = asyncio.Event()

    async def on_motion(ev):
        await client.start_livestream(ev["serialNumber"])

    client.on("device", "motion detected", on_motion)
    client.on("device", "livestream started", lambda ev: started.set())
    client.start()
    try:
        assert await _wait(lambda: client.connected and client.driver_connected)
        await server.emit({"source": "device", "event": "motion detected", "serialNumber": "T8160TEST", "state": True})
        await asyncio.wait_for(started.wait(), 5)
        assert any(c["command"] == "device.start_livestream" for c in server.commands)
    finally:
        await client.stop()


async def test_verify_code_flow(server):
    server.driver_connected = False
    client = EufyWsClient(server.url)
    client.start()
    try:
        assert await _wait(lambda: client.connected and server.commands and server.commands[-1]["command"] == "start_listening")
        assert not client.driver_connected
        await server.emit({"source": "driver", "event": "verify code"})
        assert await _wait(lambda: client.needs_verify_code)
        await client.set_verify_code("123456")
        # driver "connected" event triggers a refresh -> devices appear
        assert await _wait(lambda: client.driver_connected and "T8160TEST" in client.devices)
        assert not client.needs_verify_code
    finally:
        await client.stop()


async def test_talkback_waits_for_confirmation(server):
    client = EufyWsClient(server.url)
    client.start()
    try:
        assert await _wait(lambda: client.driver_connected)
        await client.start_talkback("T8160TEST")
        assert "T8160TEST" in client.talkbacks
    finally:
        await client.stop()
