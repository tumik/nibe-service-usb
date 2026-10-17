import asyncio
import base64
import json
import secrets
from types import SimpleNamespace

import pytest
from test_api_commands import FakeHeating, FakeHotWater, FakePoller, FakeState, FakeWatchdog

from nibe_service_usb import __version__
from nibe_service_usb.api import auth
from nibe_service_usb.api.commands import CommandContext
from nibe_service_usb.api.server import TcpServer
from nibe_service_usb.core.events import EventBus

KEY = bytes(range(32))
SERVER_ID = "6f1c2a9e-0000-4000-8000-000000000000"


def make_config(max_clients=4, idle_timeout=600.0):
    return SimpleNamespace(
        server=SimpleNamespace(
            listen_host="127.0.0.1", listen_port=0, max_clients=max_clients, idle_timeout=idle_timeout
        ),
        poll=SimpleNamespace(interval=600.0),
    )


def make_ctx(bus):
    state = FakeState()
    state.clients = 0
    return CommandContext(
        config=make_config(),
        state=state,
        pump=None,
        poller=FakePoller(),
        heating=FakeHeating(),
        hot_water=FakeHotWater(),
        watchdog=FakeWatchdog(),
        keystore=SimpleNamespace(key=KEY, server_id=SERVER_ID),
        bus=bus,
    )


class Client:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.hello = None
        self._ids = 0

    @classmethod
    async def connect(cls, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        client = cls(reader, writer)
        client.hello = await client.recv()
        return client

    async def recv(self, wait=2.0):
        line = await asyncio.wait_for(self.reader.readline(), wait)
        return json.loads(line) if line else None

    def send_raw(self, data: bytes):
        self.writer.write(data)

    async def request(self, cmd, params=None, id=None):
        self._ids += 1
        msg = {"id": self._ids if id is None else id, "cmd": cmd}
        if params is not None:
            msg["params"] = params
        self.writer.write(json.dumps(msg).encode() + b"\n")
        await self.writer.drain()
        return msg["id"]

    async def call(self, cmd, params=None):
        id_ = await self.request(cmd, params)
        while True:
            msg = await self.recv()
            if msg is None or msg.get("id") == id_:
                return msg

    async def authenticate(self, key=KEY, proto=1):
        client_nonce = base64.b64encode(secrets.token_bytes(32)).decode()
        proof = auth.compute_proof(key, self.hello["data"]["nonce"], client_nonce)
        reply = await self.call(
            "auth", {"proto": proto, "client": "test/1", "client_nonce": client_nonce, "proof": proof}
        )
        return reply, client_nonce

    async def eof(self, wait=3.0):
        while True:
            line = await asyncio.wait_for(self.reader.readline(), wait)
            if not line:
                return

    def close(self):
        self.writer.close()


@pytest.fixture
async def env():
    bus = EventBus()
    ctx = make_ctx(bus)
    server = TcpServer(
        make_config(),
        ctx,
        auth_fail_delay=0.0,
        limiter=auth.AuthLimiter(delay=0.0),
    )
    await server.start()
    clients = []

    async def connect():
        c = await Client.connect(server.port)
        clients.append(c)
        return c

    async def login():
        c = await connect()
        reply, _ = await c.authenticate()
        assert reply["ok"] is True
        return c

    yield SimpleNamespace(server=server, ctx=ctx, bus=bus, connect=connect, login=login)
    for c in clients:
        c.close()
    await server.stop()


async def test_hello_event(env):
    c = await env.connect()
    hello = c.hello
    assert hello["event"] == "hello"
    assert isinstance(hello["ts"], float)
    data = hello["data"]
    assert data["server"] == "nibe-service-usb"
    assert data["version"] == __version__
    assert data["proto"] == 1
    assert data["server_id"] == SERVER_ID
    assert data["auth"] == "hmac-sha256"
    assert len(base64.b64decode(data["nonce"])) == 32


async def test_nonce_differs_per_connection(env):
    a, b = await env.connect(), await env.connect()
    assert a.hello["data"]["nonce"] != b.hello["data"]["nonce"]


async def test_successful_handshake_with_mutual_proof(env):
    c = await env.connect()
    reply, client_nonce = await c.authenticate()
    assert reply["ok"] is True
    sp = reply["data"]["server_proof"]
    assert auth.verify_server_proof(KEY, c.hello["data"]["nonce"], client_nonce, sp)
    assert env.ctx.state.clients == 1
    assert env.ctx.watchdog.beats >= 1
    pong = await c.call("ping")
    assert pong["ok"] is True and pong["data"]["pump_online"] is True


async def test_wrong_key_fails_and_closes(env):
    c = await env.connect()
    reply, _ = await c.authenticate(key=bytes(32))
    assert reply["ok"] is False
    assert reply["error"]["code"] == "auth_failed"
    await c.eof()
    assert env.ctx.state.clients == 0


async def test_unsupported_proto_fails(env):
    c = await env.connect()
    reply, _ = await c.authenticate(proto=2)
    assert reply["error"]["code"] == "auth_failed"
    await c.eof()


async def test_invalid_auth_params(env):
    c = await env.connect()
    reply = await c.call("auth", {"proto": 1})
    assert reply["error"]["code"] == "invalid_params"
    await c.eof()


async def test_short_client_nonce_rejected(env):
    c = await env.connect()
    nonce = base64.b64encode(b"short").decode()
    proof = auth.compute_proof(KEY, c.hello["data"]["nonce"], nonce)
    reply = await c.call("auth", {"proto": 1, "client": "x", "client_nonce": nonce, "proof": proof})
    assert reply["error"]["code"] == "auth_failed"


async def test_command_before_auth_is_refused_and_closed(env):
    c = await env.connect()
    reply = await c.call("ping")
    assert reply["ok"] is False
    assert reply["error"]["code"] == "auth_required"
    await c.eof()
    assert env.ctx.watchdog.beats == 0


async def test_auth_twice_is_refused(env):
    c = await env.login()
    reply = await c.call("auth", {"proto": 1, "client": "x", "client_nonce": "AA==", "proof": "00"})
    assert reply["ok"] is False and reply["error"]["code"] == "invalid_params"


async def test_lockout_after_repeated_failures():
    bus = EventBus()
    ctx = make_ctx(bus)
    limiter = auth.AuthLimiter(max_failures=2, delay=0.0)
    server = TcpServer(make_config(), ctx, auth_fail_delay=0.0, limiter=limiter)
    await server.start()
    try:
        for _ in range(2):
            c = await Client.connect(server.port)
            reply, _ = await c.authenticate(key=bytes(32))
            assert reply["error"]["code"] == "auth_failed"
            c.close()
        assert limiter.is_locked("127.0.0.1")
        # even the right key is now refused
        c = await Client.connect(server.port)
        reply, _ = await c.authenticate()
        assert reply["error"]["code"] == "auth_failed"
        c.close()
    finally:
        await server.stop()


async def test_pipelined_ping_not_blocked_by_slow_command(env):
    c = await env.login()
    gate = asyncio.Event()

    async def slow_set_mode(mode):
        await gate.wait()
        return {"mode": mode}

    env.ctx.hot_water.set_mode = slow_set_mode
    slow_id = await c.request("set_hot_water_mode", {"mode": "lux"}, id=100)
    ping_id = await c.request("ping", id=101)
    first = await c.recv()
    assert first["id"] == ping_id == 101 and first["ok"] is True
    gate.set()
    second = await c.recv()
    assert second == {"id": slow_id, "ok": True, "data": {"mode": "lux"}}


async def test_pipelined_requests_all_answered(env):
    c = await env.login()
    for i in range(10, 20):
        await c.request("ping", id=i)
    ids = {(await c.recv())["id"] for _ in range(10)}
    assert ids == set(range(10, 20))


async def test_unknown_command_and_invalid_params(env):
    c = await env.login()
    assert (await c.call("frobnicate"))["error"]["code"] == "unknown_command"
    reply = await c.call("set_hot_water_mode", {"mode": "scalding"})
    assert reply["error"]["code"] == "invalid_params"
    assert "mode" in reply["error"]["message"]
    assert (await c.call("ping"))["ok"] is True  # connection still usable


async def test_unknown_top_level_keys_ignored(env):
    c = await env.login()
    c.send_raw(b'{"id":7,"cmd":"ping","future":true}\n')
    msg = await c.recv()
    assert msg["id"] == 7 and msg["ok"] is True


async def test_malformed_lines_closed_after_three(env):
    c = await env.login()
    for _ in range(2):
        c.send_raw(b"garbage\n")
        msg = await c.recv()
        assert msg["id"] is None and msg["ok"] is False
    assert (await c.call("ping"))["ok"] is True
    # the third malformed line (the counter is not reset by good requests) is answered, then closes
    c.send_raw(b"garbage\n")
    msg = await c.recv()
    assert msg["id"] is None and msg["ok"] is False
    await c.eof()


async def test_non_request_object_is_malformed(env):
    c = await env.login()
    c.send_raw(b'{"hello":1}\n')
    msg = await c.recv()
    assert msg["id"] is None and msg["ok"] is False


async def test_line_too_long_closes_connection(env):
    c = await env.login()
    c.send_raw(b'{"id":1,"cmd":"ping","pad":"' + b"a" * 70000 + b'"}\n')
    await c.eof()


async def test_subscribe_and_sensor_update_filtering(env):
    a = await env.login()
    b = await env.login()
    unsub = await env.login()
    reply = await a.call("subscribe", {"sensors": ["*"]})
    assert reply["ok"] and "outdoor_temp" in reply["data"]["sensors"]
    assert reply["data"]["poll_interval"] == 600.0
    await b.call("subscribe", {"sensors": ["outdoor_temp"]})
    assert env.ctx.poller.fresh == 2

    await env.bus.publish("sensor_changed", {"values": {"outdoor_temp": -3.6, "degree_minutes": -130}})
    ev_a = await a.recv()
    assert ev_a["event"] == "sensor_update"
    assert ev_a["data"] == {"values": {"outdoor_temp": -3.6, "degree_minutes": -130}}
    assert isinstance(ev_a["ts"], float)
    ev_b = await b.recv()
    assert ev_b["data"] == {"values": {"outdoor_temp": -3.6}}

    # b is not interested in this one, a is; unsubscribed client gets nothing
    await env.bus.publish("sensor_changed", {"values": {"degree_minutes": -140}})
    assert (await a.recv())["data"]["values"] == {"degree_minutes": -140}
    await b.call("unsubscribe")
    await env.bus.publish("sensor_changed", {"values": {"outdoor_temp": 1.0}})
    assert (await a.recv())["data"]["values"] == {"outdoor_temp": 1.0}
    assert (await b.call("ping"))["ok"]  # nothing queued in front of the pong
    assert (await unsub.call("ping"))["ok"]


async def test_status_event_to_all_authenticated(env):
    a = await env.login()
    b = await env.login()
    pre = await env.connect()  # not authenticated: must not receive it
    await env.bus.publish("status_changed", {"version": "0.1.0", "pump": {"online": False}})
    for c in (a, b):
        ev = await c.recv()
        assert ev["event"] == "status"
        assert ev["data"]["pump"] == {"online": False}
        assert ev["data"]["clients"] == 2
    with pytest.raises(TimeoutError):
        await pre.recv(wait=0.2)


async def test_shutdown_event_via_bus_closes_connection(env):
    c = await env.login()
    await env.bus.publish("shutdown", {"reason": "sigterm"})
    ev = await c.recv()
    assert ev["event"] == "shutdown" and ev["data"] == {"reason": "sigterm"}
    await c.eof()


async def test_stop_sends_shutdown_and_closes():
    bus = EventBus()
    ctx = make_ctx(bus)
    server = TcpServer(make_config(), ctx, auth_fail_delay=0.0)
    await server.start()
    c = await Client.connect(server.port)
    reply, _ = await c.authenticate()
    assert reply["ok"]
    await server.stop("restart")
    ev = await c.recv()
    assert ev["event"] == "shutdown" and ev["data"]["reason"] == "restart"
    await c.eof()
    assert ctx.state.clients == 0
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", server.port)


async def test_handshake_deadline():
    bus = EventBus()
    server = TcpServer(make_config(), make_ctx(bus), handshake_timeout=0.2)
    await server.start()
    try:
        c = await Client.connect(server.port)  # receives hello, then stays silent
        await c.eof(wait=2.0)
    finally:
        await server.stop()


async def test_idle_timeout_closes_authenticated_connection():
    bus = EventBus()
    server = TcpServer(make_config(idle_timeout=0.2), make_ctx(bus), auth_fail_delay=0.0)
    await server.start()
    try:
        c = await Client.connect(server.port)
        assert (await c.authenticate())[0]["ok"]
        await c.eof(wait=2.0)
    finally:
        await server.stop()


async def test_max_authenticated_clients():
    bus = EventBus()
    ctx = make_ctx(bus)
    server = TcpServer(make_config(max_clients=2), ctx, auth_fail_delay=0.0)
    await server.start()
    clients = []
    try:
        for _ in range(2):
            c = await Client.connect(server.port)
            assert (await c.authenticate())[0]["ok"]
            clients.append(c)
        assert ctx.state.clients == 2
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        assert await asyncio.wait_for(reader.readline(), 2) == b""  # closed without hello
        writer.close()
        # a freed slot is usable again
        clients[0].close()
        await asyncio.sleep(0.1)
        assert ctx.state.clients == 1
        c = await Client.connect(server.port)
        assert (await c.authenticate())[0]["ok"]
        clients.append(c)
    finally:
        for c in clients:
            c.close()
        await server.stop()


async def test_max_total_sockets():
    bus = EventBus()
    server = TcpServer(make_config(max_clients=1), make_ctx(bus), handshake_timeout=5)
    await server.start()
    try:
        first = await Client.connect(server.port)
        third = await Client.connect(server.port)  # 2 unauthenticated sockets = 2 * max_clients
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        assert await asyncio.wait_for(reader.readline(), 2) == b""
        writer.close()
        first.close()
        third.close()
    finally:
        await server.stop()


async def test_slow_consumer_is_disconnected():
    from nibe_service_usb.api.session import Session

    class StuckWriter:
        closed = False

        def write(self, data):
            pass

        async def drain(self):
            await asyncio.sleep(10)

        def close(self):
            self.closed = True

    writer = StuckWriter()
    session = Session(1, "x", writer, send_timeout=0.1)  # type: ignore[arg-type]
    await session.send({"event": "x"})
    assert session.dead and writer.closed
    await session.send({"event": "y"})  # no-op once dead


async def test_session_send_error_marks_dead():
    from nibe_service_usb.api.session import Session

    class BrokenWriter:
        def write(self, data):
            raise ConnectionResetError

        def close(self):
            pass

    session = Session(1, "x", BrokenWriter())  # type: ignore[arg-type]
    await session.send({"a": 1})
    assert session.dead


async def test_full_flow_commands(env):
    c = await env.login()
    assert (await c.call("set_hot_water_mode", {"mode": "lux"}))["data"] == {"mode": "lux"}
    assert (await c.call("set_heating_request", {"mode": "auto"}))["ok"]
    status = await c.call("get_status")
    assert status["data"]["clients"] == 1
    assert (await c.call("refresh"))["data"] == {"queued": True}
    assert (await c.call("refresh"))["error"]["code"] == "rate_limited"
