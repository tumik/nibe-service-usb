import asyncio
import json
import logging
import re
from importlib import resources
from types import SimpleNamespace

import pytest
from test_api_commands import FakeHeating, FakeHotWater, FakePoller, FakeState, FakeWatchdog

from nibe_service_usb.api.commands import COMMANDS, CommandContext
from nibe_service_usb.core.events import EventBus
from nibe_service_usb.debug.web import MAX_REQUEST_BYTES, DebugWebServer
from nibe_service_usb.pump.interface import PumpOffline


class RichFakeState(FakeState):
    """FakeState whose snapshot also carries the sensors, like the real State."""

    def snapshot(self):
        snap = super().snapshot()
        snap["sensors"] = self.sensor_snapshot()
        return snap


def make_ctx(bus):
    state = RichFakeState()
    state.clients = 0
    return CommandContext(
        config=SimpleNamespace(poll=SimpleNamespace(interval=600.0)),
        state=state,
        pump=None,
        poller=FakePoller(),
        heating=FakeHeating(),
        hot_water=FakeHotWater(),
        watchdog=FakeWatchdog(),
        keystore=None,
        bus=bus,
    )


def make_config(feeds_watchdog=False, host="127.0.0.1", port=0):
    return SimpleNamespace(enabled=True, host=host, port=port, feeds_watchdog=feeds_watchdog)


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def ctx(bus):
    return make_ctx(bus)


@pytest.fixture
async def make_server(ctx, bus):
    servers = []

    async def factory(**cfg):
        server = DebugWebServer(make_config(**cfg), ctx, bus)
        await server.start()
        servers.append(server)
        return server

    yield factory
    for server in servers:
        await server.stop()


@pytest.fixture
async def server(make_server):
    return await make_server()


# ------------------------------------------------------------------ helpers


async def raw_request(port, data: bytes, wait=3.0):
    """Send raw bytes, read until the server closes; returns (status, headers, body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(-1), wait)
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers, body


async def request(port, method, path, body: bytes | None = None, headers: dict | None = None):
    hdrs = {"Host": f"127.0.0.1:{port}"}
    if body is not None:
        hdrs["Content-Length"] = str(len(body))
    hdrs.update(headers or {})
    head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n"
    return await raw_request(port, head.encode() + (body or b""))


async def post_cmd(port, cmd, params=None, **extra):
    payload = {"cmd": cmd}
    if params is not None:
        payload["params"] = params
    status, headers, body = await request(
        port, "POST", "/cmd", json.dumps(payload).encode(), {"Content-Type": "application/json", **extra}
    )
    return status, json.loads(body)


class Stream:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    @classmethod
    async def open(cls, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET /events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await writer.drain()
        stream = cls(reader, writer)
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        stream.head = head.decode()
        return stream

    async def block(self, wait=3.0) -> str:
        raw = await asyncio.wait_for(self.reader.readuntil(b"\n\n"), wait)
        return raw.decode()

    async def event(self, wait=3.0):
        """Next real event as (name, data); skips comments and retry lines."""
        while True:
            block = await self.block(wait)
            name, data = None, None
            for line in block.splitlines():
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
            if name:
                return name, data

    def close(self):
        self.writer.close()


# ------------------------------------------------------------------ static page


async def test_index_page(server):
    status, headers, body = await request(server.port, "GET", "/")
    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert int(headers["content-length"]) == len(body)
    assert headers["connection"] == "close"
    assert "default-src 'none'" in headers["content-security-policy"]
    assert "Controls" in body.decode()


def test_html_has_no_external_resources():
    html = resources.files("nibe_service_usb.debug").joinpath("index.html").read_text(encoding="utf-8")
    assert not re.search(r"(?:src|href|action|poster)\s*=\s*[\"']?\s*(?:https?:)?//", html, re.I)
    assert not re.search(r"url\(\s*[\"']?\s*(?:https?:)?//", html, re.I)
    assert "@import" not in html
    assert "http://" not in html and "https://" not in html


def test_url_uses_bound_port(server):
    assert server.port != 0
    assert server.url == f"http://127.0.0.1:{server.port}"


# ------------------------------------------------------------------ /state


async def test_state(server):
    status, headers, body = await request(server.port, "GET", "/state")
    assert status == 200
    assert headers["content-type"] == "application/json"
    data = json.loads(body)
    assert data["status"]["version"] == "0.1.0"
    assert "uptime" in data["status"]
    assert set(data["sensors"]) == {"outdoor_temp", "hot_water_mode"}
    assert data["commands"] == sorted(COMMANDS)
    assert data["feeds_watchdog"] is False
    assert isinstance(data["now"], float)


# ------------------------------------------------------------------ /events


async def test_events_snapshot_then_updates(server, bus):
    stream = await Stream.open(server.port)
    try:
        assert "text/event-stream" in stream.head
        assert "200 OK" in stream.head
        name, data = await stream.event()
        assert name == "snapshot"
        assert set(data) >= {"status", "sensors", "commands", "now"}

        await bus.publish("sensor_changed", {"values": {"outdoor_temp": -4.0}})
        name, data = await stream.event()
        assert name == "sensor_update"
        assert data["values"] == {"outdoor_temp": -4.0}
        assert isinstance(data["ts"], float)

        await bus.publish("status_changed", {"version": "0.1.0", "pump": {"online": False}})
        assert await stream.event() == ("status", {"version": "0.1.0", "pump": {"online": False}})

        await bus.publish("pump_state", {"online": True})
        assert await stream.event() == ("pump_state", {"online": True})

        await bus.publish("shutdown", {"reason": "sigterm"})
        assert await stream.event() == ("shutdown", {"reason": "sigterm"})
        assert await asyncio.wait_for(stream.reader.read(), 3) == b""  # stream ends after shutdown
    finally:
        stream.close()


async def test_events_unsubscribes_on_disconnect(server, bus):
    stream = await Stream.open(server.port)
    await stream.event()
    assert any(bus._subscribers.get(t) for t in ("sensor_changed", "status_changed"))
    stream.close()
    for _ in range(100):
        if not any(
            bus._subscribers.get(t) for t in ("sensor_changed", "status_changed", "pump_state", "shutdown")
        ):
            break
        await asyncio.sleep(0.02)
    assert not any(
        bus._subscribers.get(t) for t in ("sensor_changed", "status_changed", "pump_state", "shutdown")
    )


async def test_events_keepalive(server):
    server.keepalive_interval = 0.05
    stream = await Stream.open(server.port)
    try:
        assert (await stream.event())[0] == "snapshot"
        assert await stream.block() == ": keep-alive\n\n"
    finally:
        stream.close()


async def test_stop_closes_event_streams(ctx, bus):
    server = DebugWebServer(make_config(), ctx, bus)
    await server.start()
    stream = await Stream.open(server.port)
    await stream.event()
    await asyncio.wait_for(server.stop(), 3)
    assert await asyncio.wait_for(stream.reader.read(), 3) == b""
    assert not bus._subscribers.get("sensor_changed")
    stream.close()


# ------------------------------------------------------------------ POST /cmd


async def test_cmd_ok(server, ctx):
    status, body = await post_cmd(server.port, "set_hot_water_mode", {"mode": "eco"})
    assert status == 200
    assert body == {"ok": True, "data": {"mode": "eco"}}
    assert ctx.hot_water.modes == ["eco"]


async def test_cmd_without_params(server, ctx):
    status, body = await post_cmd(server.port, "trigger_hot_water_boost")
    assert status == 200 and body["ok"] is True
    assert body["data"]["started"] is True
    assert ctx.hot_water.triggers == 1


async def test_cmd_heating_and_refresh(server, ctx):
    _, body = await post_cmd(server.port, "set_heating_request", {"mode": "disable"})
    assert body["ok"] is True and body["data"]["mode"] == "disable"
    assert ctx.heating.modes == ["disable"]
    _, body = await post_cmd(server.port, "refresh", {})
    assert body == {"ok": True, "data": {"queued": True}}
    await asyncio.wait_for(ctx.poller.polled.wait(), 2)


async def test_cmd_unknown_command(server, ctx):
    status, body = await post_cmd(server.port, "read_register", {"register": 40004})
    assert status == 200
    assert body["ok"] is False
    assert body["error"]["code"] == "unknown_command"


async def test_cmd_invalid_params(server, ctx):
    status, body = await post_cmd(server.port, "set_hot_water_mode", {"mode": "turbo"})
    assert status == 200 and body["ok"] is False
    assert body["error"]["code"] == "invalid_params"
    assert "mode" in body["error"]["message"]
    assert ctx.hot_water.modes == []


async def test_cmd_service_error_is_reported(server, ctx):
    ctx.hot_water.error = PumpOffline("serial down")
    status, body = await post_cmd(server.port, "set_hot_water_mode", {"mode": "lux"})
    assert status == 200
    assert body["ok"] is False and body["error"]["code"] == "pump_offline"
    assert "serial down" in body["error"]["message"]


async def test_cmd_wrong_shape_is_a_command_error(server):
    for payload in (b"[1, 2]", b'{"params": {}}', b'{"cmd": 5}', b'"cmd"'):
        status, _, body = await request(
            server.port, "POST", "/cmd", payload, {"Content-Type": "application/json"}
        )
        assert status == 200
        assert json.loads(body)["error"]["code"] == "invalid_params"
    status, body = await post_cmd(server.port, "set_hot_water_mode", [1])
    assert body["error"]["code"] == "invalid_params"


async def test_cmd_invalid_json_is_400(server):
    for payload in (b"{not json", b"", b"\xff\xfe"):
        status, headers, body = await request(
            server.port, "POST", "/cmd", payload, {"Content-Type": "application/json"}
        )
        assert status == 400
        assert json.loads(body)["ok"] is False


async def test_cmd_requires_json_content_type(server, ctx):
    for ctype in ({}, {"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"}):
        status, _, _ = await request(server.port, "POST", "/cmd", b'{"cmd":"ping"}', ctype)
        assert status == 415
    assert ctx.watchdog.beats == 0


async def test_cmd_origin_check(server, ctx):
    host = f"127.0.0.1:{server.port}"
    status, _ = await post_cmd(server.port, "ping", None, Origin="http://evil.example")
    assert status == 403
    status, _ = await post_cmd(server.port, "ping", None, Origin="null")
    assert status == 403
    status, body = await post_cmd(server.port, "ping", None, Origin=f"http://{host}")
    assert status == 200 and body["ok"] is True


@pytest.mark.parametrize("feeds, beats", [(False, 0), (True, 2)])
async def test_watchdog_feeding(make_server, ctx, feeds, beats):
    server = await make_server(feeds_watchdog=feeds)
    await post_cmd(server.port, "ping", {})
    await post_cmd(server.port, "set_hot_water_mode", {"mode": "normal"})
    await post_cmd(server.port, "no_such_command", {})  # unknown commands never feed the watchdog
    assert ctx.watchdog.beats == beats
    assert ctx.hot_water.modes == ["normal"]


async def test_cmd_logged_with_client_address(server, caplog):
    caplog.set_level(logging.INFO, logger="nibe_service_usb.debug.web")
    await post_cmd(server.port, "set_hot_water_mode", {"mode": "lux"})
    messages = [r.getMessage() for r in caplog.records]
    assert any("set_hot_water_mode" in m and "127.0.0.1:" in m and "ok" in m for m in messages)


async def test_rate_limit_state_shared_with_original_context(server, ctx):
    await post_cmd(server.port, "refresh", {})
    _, body = await post_cmd(server.port, "refresh", {})
    assert body["error"]["code"] == "rate_limited"
    assert "refresh" in ctx.last_called


# ------------------------------------------------------------------ HTTP details


async def test_unknown_path_is_404(server):
    for path in ("/nope", "/cmd/x", "/index.html", "/events/"):
        status, _, body = await request(server.port, "GET", path)
        assert status == 404
        assert json.loads(body)["ok"] is False


async def test_query_string_is_ignored(server):
    status, _, _ = await request(server.port, "GET", "/state?x=1")
    assert status == 200


async def test_wrong_method_is_405(server):
    status, headers, _ = await request(server.port, "POST", "/state", b"{}")
    assert status == 405 and headers["allow"] == "GET"
    status, headers, _ = await request(server.port, "GET", "/cmd")
    assert status == 405 and headers["allow"] == "POST"
    status, _, _ = await request(server.port, "DELETE", "/")
    assert status == 405


async def test_malformed_request_line_is_400(server):
    status, _, _ = await raw_request(server.port, b"BANANA\r\n\r\n")
    assert status == 400


async def test_post_without_content_length_is_411(server):
    status, _, _ = await raw_request(server.port, b"POST /cmd HTTP/1.1\r\nHost: x\r\n\r\n")
    assert status == 411


async def test_request_size_cap_body(server, ctx):
    status, _, _ = await request(
        server.port,
        "POST",
        "/cmd",
        None,
        {"Content-Type": "application/json", "Content-Length": str(MAX_REQUEST_BYTES + 1)},
    )
    assert status == 413
    assert ctx.hot_water.modes == []


async def test_request_size_cap_just_under_limit_is_accepted(server):
    filler = " " * (MAX_REQUEST_BYTES - 1024)
    body = ('{"cmd":"ping"' + filler + "}").encode()
    status, _, raw = await request(server.port, "POST", "/cmd", body, {"Content-Type": "application/json"})
    assert status == 200 and json.loads(raw)["ok"] is True


async def test_request_size_cap_headers(server):
    huge = "X-Junk: " + "a" * 1000 + "\r\n"
    data = b"GET / HTTP/1.1\r\nHost: x\r\n" + (huge * 100).encode() + b"\r\n"
    status, _, _ = await raw_request(server.port, data)
    assert status == 431
    long_line = b"GET /" + b"a" * (MAX_REQUEST_BYTES + 10) + b" HTTP/1.1\r\n\r\n"
    status, _, _ = await raw_request(server.port, long_line)
    assert status == 431


async def test_chunked_request_is_refused(server):
    data = b"POST /cmd HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
    status, _, _ = await raw_request(server.port, data)
    assert status == 501


async def test_stalled_client_does_not_block_others(server):
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(b"GET /sta")  # never finished
    status, _, _ = await request(server.port, "GET", "/state")
    assert status == 200
    writer.close()


# ------------------------------------------------------------------ startup


async def test_warns_when_bound_to_non_loopback(ctx, bus, caplog):
    caplog.set_level(logging.WARNING, logger="nibe_service_usb.debug.web")
    server = DebugWebServer(make_config(), ctx, bus, host="0.0.0.0", port=0)
    await server.start()
    try:
        assert any("no authentication" in r.getMessage() for r in caplog.records)
    finally:
        await server.stop()


async def test_no_warning_on_loopback(server, caplog):
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_constructor_overrides_host_and_port(ctx, bus):
    server = DebugWebServer(make_config(host="10.9.9.9", port=1), ctx, bus, host="127.0.0.1", port=0)
    await server.start()
    try:
        assert server.url.startswith("http://127.0.0.1:") and server.port > 1
    finally:
        await server.stop()
