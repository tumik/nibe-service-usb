from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nibe_service_usb.app import App
from nibe_service_usb.config import Config, load_config
from nibe_service_usb.pump.registers import load_sensor_defs
from nibe_service_usb.pump.simulator import SimulatedPumpClient

OVERRIDES = {
    "server.listen_host": "127.0.0.1",
    "server.listen_port": 0,
    "discovery.enabled": False,
}


def make_config(tmp_path: Path, **extra: object) -> Config:
    return load_config(None, data_dir=tmp_path / "data", overrides={**OVERRIDES, **extra})


class RecordingPump(SimulatedPumpClient):
    """Simulator that records every write attempt."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.writes: list[tuple] = []

    async def write(self, register, value, **kwargs):
        self.writes.append(("write", register, value))
        return await super().write(register, value, **kwargs)

    async def write_ram(self, address, data, **kwargs):
        self.writes.append(("write_ram", address, bytes(data)))
        return await super().write_ram(address, data, **kwargs)


def make_app(tmp_path: Path, **extra: object) -> tuple[App, RecordingPump]:
    config = make_config(tmp_path, **extra)
    pump = RecordingPump(load_sensor_defs(None), realtime=False)
    return App(config, pump=pump), pump


async def wait_for_sensor(app: App, name: str, limit: float = 5.0) -> None:
    async def poll() -> None:
        while app.state.get(name) is None:  # noqa: ASYNC110
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), limit)


def other_tasks() -> set[asyncio.Task]:
    return {t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()}


async def test_start_poll_and_stop(tmp_path: Path) -> None:
    app, pump = make_app(tmp_path)
    await app.start()
    try:
        assert app.ready.is_set()
        assert pump.online
        assert app.tcp_port > 0
        assert app.ctx.state is app.state
        assert app.mdns is None
        await wait_for_sensor(app, "bt1_outdoor")
        assert app.state.get("bt1_outdoor") == 4.7
        assert app.state.pump.online is True
        assert app.state.identity.product == "F1226-6 R"
    finally:
        await app.stop()
    await asyncio.sleep(0)
    assert other_tasks() == set()


async def test_stop_writes_nothing_and_is_idempotent(tmp_path: Path) -> None:
    app, pump = make_app(tmp_path)
    await app.start()
    await wait_for_sensor(app, "bt1_outdoor")
    pump.writes.clear()
    await app.stop()
    await app.stop()
    assert pump.writes == []
    assert pump.registers[40969] == 96859  # no EEPROM store call either


async def test_run_returns_after_stop(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path)
    task = asyncio.create_task(app.run())
    await asyncio.wait_for(app.ready.wait(), 5)
    await app.stop()
    await asyncio.wait_for(task, 5)
    assert other_tasks() == set()


async def test_stop_continues_after_a_failing_step(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    app, pump = make_app(tmp_path)
    await app.start()
    order: list[str] = []

    async def boom() -> None:
        order.append("boom")
        raise RuntimeError("broken")

    stack = [(name, stopper) for name, stopper in app._stack]
    app._stack = [(name, _wrap(order, name, stopper)) for name, stopper in stack]
    app._stack[-1] = ("tcp server", boom)
    await app.stop()
    assert order[0] == "boom"
    assert order[-1] == "pump"  # reverse start order, pump last
    assert "Error stopping tcp server" in caplog.text
    assert not pump.online


def _wrap(order: list[str], name: str, stopper):
    async def run() -> None:
        order.append(name)
        await stopper()

    return run


async def test_diagnostic_sensors_hidden_by_default(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path)
    diagnostic = [s.name for s in load_sensor_defs(None) if s.tier == "diagnostic"]
    assert diagnostic, "sensors.yaml has no diagnostic sensors to check"
    assert all(app.state.sensor_def(name) is None for name in diagnostic)
    config = make_config(tmp_path / "x", **{"poll.diagnostic": True})
    app2 = App(config, pump=SimulatedPumpClient(load_sensor_defs(None)))
    assert all(app2.state.sensor_def(name) is not None for name in diagnostic)


async def test_watchdog_timeout_reverts_to_auto(tmp_path: Path) -> None:
    app, pump = make_app(tmp_path)
    await app.start()
    try:
        await wait_for_sensor(app, "bt1_outdoor")
        await app.heating.set_mode("disable")
        await app.hot_water.set_mode("lux")
        assert pump.registers[47041] == 2
        await app._on_watchdog_timeout()
        assert app.state.heating.mode == "auto"
        assert pump.registers[47041] == 1  # defaults.hot_water_mode = "normal"
    finally:
        await app.stop()


async def test_pump_back_online_reevaluates_heating(tmp_path: Path) -> None:
    app, pump = make_app(tmp_path)
    await app.start()
    try:
        await wait_for_sensor(app, "bt1_outdoor")
        called = []
        original = app.heating.on_pump_online

        async def spy() -> None:
            called.append(1)
            await original()

        app.heating.on_pump_online = spy  # type: ignore[method-assign]
        pump.unplug()
        await asyncio.sleep(0.05)
        assert called == []
        pump.plug()
        await asyncio.wait_for(_until(lambda: bool(called)), 5)
    finally:
        await app.stop()


async def _until(predicate) -> None:
    while not predicate():  # noqa: ASYNC110
        await asyncio.sleep(0.02)


async def test_debug_website_started_when_enabled(tmp_path: Path) -> None:
    pytest.importorskip("nibe_service_usb.debug.web")
    app, _ = make_app(tmp_path, **{"debug_website.enabled": True, "debug_website.port": 0})
    await app.start()
    try:
        assert app.debug_web is not None
        assert app.debug_url and app.debug_url.startswith("http://")
    finally:
        await app.stop()
    await asyncio.sleep(0)
    assert other_tasks() == set()
