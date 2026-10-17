import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from nibe_service_usb.api import commands
from nibe_service_usb.api.commands import (
    COMMANDS,
    CommandContext,
    CommandSpec,
    ParamSpec,
    dispatch,
    validate_params,
)
from nibe_service_usb.api.session import Session
from nibe_service_usb.core.errors import CommandError
from nibe_service_usb.pump.interface import PumpOffline, PumpTimeout, WriteFailed

SNAPSHOT = {
    "outdoor_temp": {"value": -3.4, "ts": 1.0, "name": "Outdoor", "kind": "temperature", "unit": "°C"},
    "hot_water_mode": {"value": "normal", "ts": 1.0, "name": "HW", "kind": "enum", "options": ["eco"]},
}


class FakeState:
    def __init__(self):
        self.pump = SimpleNamespace(online=True)
        self.watchdog = SimpleNamespace(ha_alive=True)
        self.clients = 2
        self.snapshot_calls = []

    def sensor_snapshot(self, names=None):
        self.snapshot_calls.append(names)
        if names is None:
            return {k: dict(v) for k, v in SNAPSHOT.items()}
        return {k: dict(v) for k, v in SNAPSHOT.items() if k in set(names)}

    def snapshot(self):
        return {"status": {"version": "0.1.0", "pump": {"online": True}}, "sensors": {}}


class FakePoller:
    def __init__(self):
        self.fresh = 0
        self.polled = asyncio.Event()
        self.poll_calls = 0

    async def ensure_fresh(self):
        self.fresh += 1

    async def poll_now(self, **kw):
        self.poll_calls += 1
        self.polled.set()


class FakeHotWater:
    def __init__(self):
        self.modes = []
        self.triggers = 0
        self.error = None

    async def set_mode(self, mode):
        if self.error:
            raise self.error
        self.modes.append(mode)
        return {"mode": mode}

    async def trigger_periodic_increase(self):
        self.triggers += 1
        if self.error:
            raise self.error
        return {"started": True, "remaining_seconds": 18000, "enabled_periodic_increase": False}


class FakeHeating:
    def __init__(self):
        self.modes = []

    async def set_mode(self, mode):
        self.modes.append(mode)
        return {"mode": mode, "active": True, "suspended_reason": None, "current_dm": -15, "written_dm": -1}


class FakeWatchdog:
    def __init__(self):
        self.beats = 0

    def heartbeat(self):
        self.beats += 1


@pytest.fixture
def ctx():
    return CommandContext(
        config=SimpleNamespace(poll=SimpleNamespace(interval=600.0)),
        state=FakeState(),
        pump=None,
        poller=FakePoller(),
        heating=FakeHeating(),
        hot_water=FakeHotWater(),
        watchdog=FakeWatchdog(),
        keystore=None,
        bus=None,
    )


@pytest.fixture
def session():
    s = Session(1, "127.0.0.1:1")
    s.authenticated = True
    return s


# ------------------------------------------------------------ validation


def spec_with(**params):
    async def handler(ctx, session, p):
        return p

    return CommandSpec(handler, params)


def test_validate_ok_and_optional():
    spec = spec_with(mode=ParamSpec("str", choices=("a", "b")), n=ParamSpec("int", required=False))
    assert validate_params(spec, {"mode": "a"}) == {"mode": "a"}
    assert validate_params(spec, {"mode": "b", "n": 3}) == {"mode": "b", "n": 3}
    assert validate_params(spec_with(), None) == {}


@pytest.mark.parametrize(
    "params, field",
    [
        ({}, "mode"),
        ({"mode": "c"}, "mode"),
        ({"mode": 1}, "mode"),
        ({"mode": "a", "other": 1}, "other"),
        ({"mode": "a", "n": "3"}, "n"),
        ({"mode": "a", "n": True}, "n"),
        ({"mode": "a", "n": 1.5}, "n"),
    ],
)
def test_validate_errors_name_the_field(params, field):
    spec = spec_with(mode=ParamSpec("str", choices=("a", "b")), n=ParamSpec("int", required=False))
    with pytest.raises(CommandError) as exc:
        validate_params(spec, params)
    assert exc.value.code == "invalid_params"
    assert field in exc.value.message


def test_validate_params_must_be_object():
    with pytest.raises(CommandError) as exc:
        validate_params(spec_with(), [1])
    assert exc.value.code == "invalid_params"


def test_validate_list_of_str():
    spec = spec_with(sensors=ParamSpec("list[str]"))
    assert validate_params(spec, {"sensors": ["a", "*"]})
    for bad in ({"sensors": "a"}, {"sensors": [1]}, {"sensors": None}):
        with pytest.raises(CommandError):
            validate_params(spec, bad)


def test_command_table():
    assert set(COMMANDS) == {
        "auth",
        "ping",
        "subscribe",
        "unsubscribe",
        "list_sensors",
        "get_status",
        "set_hot_water_mode",
        "trigger_hot_water_boost",
        "set_heating_request",
        "refresh",
    }
    assert [n for n, s in COMMANDS.items() if not s.auth_required] == ["auth"]


# ------------------------------------------------------------ dispatch


async def test_unknown_command(ctx, session):
    with pytest.raises(CommandError) as exc:
        await dispatch(ctx, session, "read_register", {})
    assert exc.value.code == "unknown_command"
    assert ctx.watchdog.beats == 0


async def test_unauthenticated_session_is_refused(ctx):
    s = Session(2, "x")
    with pytest.raises(CommandError) as exc:
        await dispatch(ctx, s, "ping", {})
    assert exc.value.code == "auth_required"
    assert ctx.watchdog.beats == 0


async def test_auth_validated_but_not_executable_after_handshake(ctx, session):
    with pytest.raises(CommandError) as exc:
        await dispatch(ctx, session, "auth", {"proto": 1})
    assert exc.value.code == "invalid_params"
    with pytest.raises(CommandError) as exc:
        await dispatch(ctx, session, "auth", {"proto": 1, "client": "x", "client_nonce": "a", "proof": "b"})
    assert exc.value.code == "invalid_params"


async def test_ping(ctx, session):
    data = await dispatch(ctx, session, "ping", {})
    assert data["pump_online"] is True and data["ha_alive"] is True
    assert data["uptime"] >= 0
    assert ctx.watchdog.beats == 1


async def test_every_authenticated_request_feeds_watchdog(ctx, session):
    await dispatch(ctx, session, "ping", None)
    await dispatch(ctx, session, "get_status", {})
    await dispatch(ctx, session, "list_sensors", {})
    assert ctx.watchdog.beats == 3


async def test_invalid_request_does_not_feed_watchdog(ctx, session):
    with pytest.raises(CommandError):
        await dispatch(ctx, session, "set_hot_water_mode", {"mode": "turbo"})
    assert ctx.watchdog.beats == 0


async def test_subscribe_all(ctx, session):
    data = await dispatch(ctx, session, "subscribe", {"sensors": ["*"]})
    assert ctx.poller.fresh == 1
    assert data["poll_interval"] == 600.0
    assert set(data["sensors"]) == set(SNAPSHOT)
    assert session.subscriptions == {"*"}
    assert ctx.state.snapshot_calls == [None]


async def test_subscribe_explicit_list_replaces(ctx, session):
    await dispatch(ctx, session, "subscribe", {"sensors": ["*"]})
    data = await dispatch(ctx, session, "subscribe", {"sensors": ["outdoor_temp", "nope"]})
    assert set(data["sensors"]) == {"outdoor_temp"}
    assert session.subscriptions == {"outdoor_temp", "nope"}
    assert session.wants_sensor("outdoor_temp") and not session.wants_sensor("hot_water_mode")


async def test_subscribe_survives_ensure_fresh_failure(ctx, session):
    async def boom():
        raise RuntimeError("pump gone")

    ctx.poller.ensure_fresh = boom
    data = await dispatch(ctx, session, "subscribe", {"sensors": ["*"]})
    assert "outdoor_temp" in data["sensors"]


async def test_unsubscribe(ctx, session):
    await dispatch(ctx, session, "subscribe", {"sensors": ["*"]})
    assert await dispatch(ctx, session, "unsubscribe", {}) == {}
    assert session.subscriptions is None
    assert not session.wants_sensor("outdoor_temp")


async def test_list_sensors_metadata_only(ctx, session):
    data = await dispatch(ctx, session, "list_sensors", {})
    assert set(data["sensors"]) == set(SNAPSHOT)
    for meta in data["sensors"].values():
        assert "value" not in meta and "ts" not in meta
    assert data["sensors"]["hot_water_mode"]["options"] == ["eco"]
    assert session.subscriptions is None


async def test_get_status(ctx, session):
    data = await dispatch(ctx, session, "get_status", {})
    assert data["version"] == "0.1.0"
    assert data["clients"] == 2
    assert "uptime" in data


async def test_set_hot_water_mode(ctx, session):
    assert await dispatch(ctx, session, "set_hot_water_mode", {"mode": "lux"}) == {"mode": "lux"}
    assert ctx.hot_water.modes == ["lux"]
    for bad in ({}, {"mode": "hot"}, {"mode": 1}):
        with pytest.raises(CommandError) as exc:
            await dispatch(ctx, session, "set_hot_water_mode", bad)
        assert exc.value.code == "invalid_params"


async def test_set_heating_request(ctx, session):
    data = await dispatch(ctx, session, "set_heating_request", {"mode": "disable"})
    assert data["written_dm"] == -1
    assert ctx.heating.modes == ["disable"]
    with pytest.raises(CommandError) as exc:
        await dispatch(ctx, session, "set_heating_request", {"mode": "on"})
    assert exc.value.code == "invalid_params"
    assert ctx.heating.modes == ["disable"]


@pytest.mark.parametrize(
    "error, code",
    [
        (CommandError("pump_offline", "x"), "pump_offline"),
        (PumpOffline("down"), "pump_offline"),
        (PumpTimeout("t"), "pump_timeout"),
        (WriteFailed("w"), "write_failed"),
    ],
)
async def test_errors_are_mapped(ctx, session, error, code):
    ctx.hot_water.error = error
    with pytest.raises(CommandError) as exc:
        await dispatch(ctx, session, "set_hot_water_mode", {"mode": "eco"})
    assert exc.value.code == code


async def test_unexpected_exception_becomes_internal_and_is_logged(ctx, session, caplog):
    ctx.hot_water.error = KeyError("boom")
    with caplog.at_level(logging.ERROR):
        with pytest.raises(CommandError) as exc:
            await dispatch(ctx, session, "set_hot_water_mode", {"mode": "eco"})
    assert exc.value.code == "internal"
    assert "boom" not in exc.value.message
    assert any(r.exc_info for r in caplog.records)


async def test_boost_rate_limited(ctx, session):
    data = await dispatch(ctx, session, "trigger_hot_water_boost", {})
    assert data["started"] is True
    with pytest.raises(CommandError) as exc:
        await dispatch(ctx, session, "trigger_hot_water_boost", {})
    assert exc.value.code == "rate_limited"
    assert ctx.hot_water.triggers == 1
    # after the interval it works again
    ctx.last_called["trigger_hot_water_boost"] = time.monotonic() - 601
    await dispatch(ctx, session, "trigger_hot_water_boost", {})
    assert ctx.hot_water.triggers == 2


async def test_failed_boost_does_not_consume_quota(ctx, session):
    ctx.hot_water.error = CommandError("trigger_failed", "timer did not start")
    with pytest.raises(CommandError) as exc:
        await dispatch(ctx, session, "trigger_hot_water_boost", {})
    assert exc.value.code == "trigger_failed"
    ctx.hot_water.error = None
    await dispatch(ctx, session, "trigger_hot_water_boost", {})
    assert ctx.hot_water.triggers == 2


async def test_refresh_queues_and_is_rate_limited(ctx, session):
    assert await dispatch(ctx, session, "refresh", {}) == {"queued": True}
    await asyncio.wait_for(ctx.poller.polled.wait(), 1)
    assert ctx.poller.poll_calls == 1
    with pytest.raises(CommandError) as exc:
        await dispatch(ctx, session, "refresh", {})
    assert exc.value.code == "rate_limited"
    ctx.last_called["refresh"] = time.monotonic() - 61
    await dispatch(ctx, session, "refresh", {})


async def test_refresh_poll_failure_is_logged_not_raised(ctx, session, caplog):
    async def boom(**kw):
        raise RuntimeError("poll broke")

    ctx.poller.poll_now = boom
    with caplog.at_level(logging.ERROR):
        assert await dispatch(ctx, session, "refresh", {}) == {"queued": True}
        await asyncio.sleep(0.05)
    assert any("Background task refresh failed" in r.getMessage() for r in caplog.records)
    assert not commands._background
