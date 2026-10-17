"""Full protocol round trip over TCP: client <-> App <-> simulated pump."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nibe_service_usb.api import auth
from nibe_service_usb.app import App
from nibe_service_usb.config import load_config
from nibe_service_usb.pump.registers import load_sensor_defs
from nibe_service_usb.pump.simulator import SimulatedPumpClient

TIMEOUT = 10.0


class Client:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.hello: dict = {}
        self.events: list[dict] = []
        self._ids = 0

    @classmethod
    async def connect(cls, port: int) -> Client:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        client = cls(reader, writer)
        client.hello = await client.recv()
        return client

    async def recv(self) -> dict:
        line = await asyncio.wait_for(self.reader.readline(), TIMEOUT)
        assert line, "connection closed"
        return json.loads(line)

    async def call(self, cmd: str, params: dict | None = None) -> dict:
        self._ids += 1
        msg: dict = {"id": self._ids, "cmd": cmd}
        if params is not None:
            msg["params"] = params
        self.writer.write(json.dumps(msg).encode() + b"\n")
        await self.writer.drain()
        while True:
            reply = await self.recv()
            if reply.get("id") == self._ids:
                return reply
            self.events.append(reply)

    async def event(self, name: str) -> dict:
        """Next event named ``name`` (buffered ones first)."""
        while True:
            for i, ev in enumerate(self.events):
                if ev.get("event") == name:
                    return self.events.pop(i)
            self.events.append(await self.recv())

    async def authenticate(self, key: bytes) -> dict:
        client_nonce = base64.b64encode(secrets.token_bytes(32)).decode()
        server_nonce = self.hello["data"]["nonce"]
        reply = await self.call(
            "auth",
            {
                "proto": 1,
                "client": "e2e/1",
                "client_nonce": client_nonce,
                "proof": auth.compute_proof(key, server_nonce, client_nonce),
            },
        )
        if reply.get("ok"):
            expected = auth.compute_server_proof(key, server_nonce, client_nonce)
            assert reply["data"]["server_proof"] == expected
        return reply

    def close(self) -> None:
        self.writer.close()


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[tuple[App, SimulatedPumpClient, list[Client]]]:
    config = load_config(
        None,
        data_dir=tmp_path / "data",
        overrides={"server.listen_host": "127.0.0.1", "server.listen_port": 0, "discovery.enabled": False},
    )
    pump = SimulatedPumpClient(load_sensor_defs(None), realtime=False)
    app = App(config, pump=pump)
    await app.start()

    async def fast_sleep(_: float) -> None:  # the boost waits 5 s between the two writes
        await asyncio.sleep(0)

    app.hot_water._sleep = fast_sleep
    clients: list[Client] = []
    try:
        yield app, pump, clients
    finally:
        for c in clients:
            c.close()
        await app.stop()


async def login(app: App, clients: list[Client]) -> Client:
    client = await Client.connect(app.tcp_port)
    clients.append(client)
    reply = await client.authenticate(app.keystore.key)
    assert reply["ok"] is True, reply
    return client


async def test_hello(env) -> None:
    app, _, clients = env
    client = await Client.connect(app.tcp_port)
    clients.append(client)
    assert client.hello["event"] == "hello"
    assert client.hello["data"]["server_id"] == app.keystore.server_id
    assert client.hello["data"]["nonce"]


async def test_full_round_trip(env) -> None:
    app, pump, clients = env
    client = await login(app, clients)

    ping = await client.call("ping")
    assert ping["ok"] and ping["data"]["pump_online"] is True and ping["data"]["ha_alive"] is True

    sub = await client.call("subscribe", {"sensors": ["*"]})
    assert sub["ok"], sub
    sensors = sub["data"]["sensors"]
    assert sensors["bt1_outdoor"]["value"] == 4.7
    assert "degree_minutes" in sensors
    assert sensors["degree_minutes"]["value"] == 0

    # Hot water mode: register 47041 and a pushed update.
    reply = await client.call("set_hot_water_mode", {"mode": "lux"})
    assert reply["ok"] and reply["data"]["mode"] == "lux", reply
    assert pump.registers[47041] == 2
    for _ in range(4):  # the first heartbeat also publishes a status event
        status_event = await client.event("status")
        if status_event["data"]["hot_water"]["mode"] == "lux":
            break
    assert status_event["data"]["hot_water"]["mode"] == "lux"

    # Heating request: with a heat demand of 0 no degree-minute write happens.
    before = pump.registers[40940]
    for mode in ("disable", "auto"):
        reply = await client.call("set_heating_request", {"mode": mode})
        assert reply["ok"] and reply["data"]["mode"] == mode, reply
        status = await client.call("get_status")
        assert status["data"]["heating"]["mode"] == mode
    assert pump.registers[40940] == before

    # Boost.
    reply = await client.call("trigger_hot_water_boost")
    assert reply["ok"] and reply["data"]["started"] is True, reply
    assert pump.periodic_timer > 0

    # Refresh.
    reply = await client.call("refresh")
    assert reply["ok"] and reply["data"]["queued"] is True

    # get_status shape.
    status = (await client.call("get_status"))["data"]
    assert status["version"]
    assert status["pump"]["online"] is True
    assert {"mode", "active", "suspended_reason"} <= set(status["heating"])
    assert {"mode", "periodic_increase"} <= set(status["hot_water"])
    assert status["watchdog"]["ha_alive"] is True
    assert status["clients"] == 1

    reply = await client.call("unsubscribe")
    assert reply["ok"]


async def test_sensor_update_event_after_register_change(env) -> None:
    app, pump, clients = env
    client = await login(app, clients)
    sub = await client.call("subscribe", {"sensors": ["bt1_outdoor"]})
    assert sub["data"]["sensors"]["bt1_outdoor"]["value"] == 4.7
    pump.set_register(40004, 100)
    await app.poller.poll_now()
    update = await client.event("sensor_update")
    assert update["data"]["values"]["bt1_outdoor"] == 10.0


async def test_bad_auth_is_refused(env) -> None:
    app, _, clients = env
    client = await Client.connect(app.tcp_port)
    clients.append(client)
    reply = await client.authenticate(bytes(32))
    assert reply["ok"] is False
    assert reply["error"]["code"] == "auth_failed"
    assert app.state.clients == 0


async def test_commands_require_auth(env) -> None:
    app, _, clients = env
    client = await Client.connect(app.tcp_port)
    clients.append(client)
    reply = await client.call("get_status")
    assert reply["ok"] is False and reply["error"]["code"] == "auth_required"


async def test_shutdown_event_on_stop(env) -> None:
    app, pump, clients = env
    client = await login(app, clients)
    await client.call("subscribe", {"sensors": ["*"]})
    await app.stop()
    event = await client.event("shutdown")
    assert event["data"]["reason"]
