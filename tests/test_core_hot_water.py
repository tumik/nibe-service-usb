from __future__ import annotations

import pytest
from test_core_fakes import PI_TIMER_ADDRESS, FakeClock, FakePump, make_config, make_state

from nibe_service_usb.core.errors import CommandError
from nibe_service_usb.core.hot_water import HotWaterController
from nibe_service_usb.core.poller import SensorPoller
from nibe_service_usb.pump.registers import load_alarm_defs, load_sensor_defs

SENSORS = load_sensor_defs(None)


class Sleeper:
    """Replaces asyncio.sleep: records delays and advances the fake monotonic clock."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.clock.advance(delay)


def make(tmp_path, registers=None):
    pump = FakePump(registers)
    config = make_config(tmp_path)
    wall = FakeClock(5000.0)
    state, bus, rec = make_state(clock=wall)
    poller = SensorPoller(config, pump, state, SENSORS, alarms=load_alarm_defs(None), clock=wall)
    mono = FakeClock(100_000.0)
    sleeper = Sleeper(mono)
    hot_water = HotWaterController(config, pump, state, poller, sleep=sleeper, monotonic=mono, clock=wall)
    return hot_water, pump, state, rec, sleeper, mono


def timer_starts_after_toggle(pump: FakePump, seconds: tuple[int, int, int] = (5, 0, 0)) -> None:
    """Like the real pump: the run timer starts when R47050 goes 0 -> 1."""

    def hook(register: int, old: int, new: int) -> None:
        if register == 47050 and old == 0 and new == 1:
            pump.ram[PI_TIMER_ADDRESS] = bytes(seconds)
            pump.registers[43109] = 3

    pump.write_hooks.append(hook)


# ----------------------------------------------------------------- comfort mode


@pytest.mark.parametrize(("mode", "value"), [("eco", 0), ("normal", 1), ("lux", 2)])
async def test_set_mode_writes_47041_and_reads_back(tmp_path, mode, value) -> None:
    hw, pump, state, rec, *_ = make(tmp_path, {47041: 1 if mode != "normal" else 0})
    result = await hw.set_mode(mode)
    assert result == {"mode": mode}
    assert pump.writes == [("reg", 47041, value)]
    assert state.hot_water.mode == mode
    assert state.get("hw_comfort") == mode
    assert state.get("hw_mode") == "normal"  # effective mode read from R43109
    assert rec.of("sensor_changed")[-1]["values"]["hw_comfort"] == mode
    assert rec.of("status_changed")[-1]["hot_water"]["mode"] == mode


async def test_set_mode_reports_effective_mode(tmp_path) -> None:
    hw, pump, state, rec, *_ = make(tmp_path)
    pump.registers[43109] = 2  # the pump says lux is in effect
    await hw.set_mode("lux")
    assert state.hot_water.effective_mode == "lux"
    assert state.get("hw_mode") == "lux"


async def test_set_mode_errors(tmp_path) -> None:
    hw, pump, state, *_ = make(tmp_path)
    with pytest.raises(CommandError) as exc:
        await hw.set_mode("boost")
    assert exc.value.code == "invalid_params"
    pump.fail_writes = True
    with pytest.raises(CommandError) as exc:
        await hw.set_mode("lux")
    assert exc.value.code == "write_failed"
    pump.fail_writes = False
    pump.go_offline()
    with pytest.raises(CommandError) as exc:
        await hw.set_mode("lux")
    assert exc.value.code == "pump_offline"


# ----------------------------------------------------------------- periodic increase


async def test_trigger_success(tmp_path) -> None:
    hw, pump, state, rec, sleeper, _ = make(tmp_path, {47050: 1})
    timer_starts_after_toggle(pump, (5, 0, 0))
    result = await hw.trigger_periodic_increase()
    assert result == {"started": True, "remaining_seconds": 18000, "enabled_periodic_increase": False}
    assert pump.writes == [("reg", 47050, 0), ("reg", 47050, 1)]
    assert sleeper.delays == [5, 3]  # 5 s between the writes, first timer poll after 3 s
    # sensors and status reflect the run immediately
    assert state.get("pi_running") is True
    assert state.get("pi_remaining") == 18000
    assert state.hot_water.periodic_increase.running is True
    assert state.hot_water.periodic_increase.remaining_seconds == 18000
    assert state.hot_water.effective_mode == "periodic"
    assert rec.of("sensor_changed")
    assert rec.of("status_changed")


async def test_trigger_reports_enabling_a_disabled_bit(tmp_path) -> None:
    hw, pump, *_ = make(tmp_path, {47050: 0})
    timer_starts_after_toggle(pump)
    result = await hw.trigger_periodic_increase()
    assert result["enabled_periodic_increase"] is True
    assert pump.registers[47050] == 1


async def test_trigger_succeeds_late_within_15_seconds(tmp_path) -> None:
    hw, pump, state, rec, sleeper, _ = make(tmp_path)
    polls = {"n": 0}
    original = pump.read_ram

    async def slow_timer(address, length, **kw):
        if address == PI_TIMER_ADDRESS:
            polls["n"] += 1
            if polls["n"] >= 3:  # 1 = pre-check, 2 = first poll (3 s), 3 = second poll (6 s)
                return bytes((0, 30, 0))
        return await original(address, length, **kw)

    pump.read_ram = slow_timer  # type: ignore[method-assign]
    result = await hw.trigger_periodic_increase()
    assert result["started"] is True
    assert result["remaining_seconds"] == 1800
    assert sleeper.delays == [5, 3, 3]


async def test_trigger_detects_start_by_effective_mode_3(tmp_path) -> None:
    hw, pump, *_ = make(tmp_path)

    def hook(register: int, old: int, new: int) -> None:
        if register == 47050 and new == 1:
            pump.registers[43109] = 3  # timer stays zero

    pump.write_hooks.append(hook)
    result = await hw.trigger_periodic_increase()
    assert result["started"] is True
    assert result["remaining_seconds"] == 0


async def test_trigger_already_running(tmp_path) -> None:
    hw, pump, state, rec, sleeper, _ = make(tmp_path)
    pump.ram[PI_TIMER_ADDRESS] = bytes((1, 2, 3))
    with pytest.raises(CommandError) as exc:
        await hw.trigger_periodic_increase()
    assert exc.value.code == "already_running"
    assert pump.writes == []
    assert sleeper.delays == []


async def test_trigger_failed_when_timer_never_starts(tmp_path) -> None:
    hw, pump, state, rec, sleeper, _ = make(tmp_path, {43109: 1, 40313: 4, 43014: 1})
    with pytest.raises(CommandError) as exc:
        await hw.trigger_periodic_increase()
    assert exc.value.code == "trigger_failed"
    assert "R43109=1" in exc.value.message
    assert "R40313=4" in exc.value.message
    assert "R43014=1" in exc.value.message
    assert sleeper.delays == [5, 3, 3, 3, 3, 3]  # polls at 3, 6, 9, 12, 15 s
    assert pump.writes == [("reg", 47050, 0), ("reg", 47050, 1)]


async def test_trigger_rate_limited_to_one_per_ten_minutes(tmp_path) -> None:
    hw, pump, state, rec, sleeper, mono = make(tmp_path)
    timer_starts_after_toggle(pump)
    await hw.trigger_periodic_increase()
    pump.ram[PI_TIMER_ADDRESS] = bytes(3)  # run ended
    pump.registers[43109] = 1
    mono.advance(60)
    with pytest.raises(CommandError) as exc:
        await hw.trigger_periodic_increase()
    assert exc.value.code == "rate_limited"
    mono.advance(600)
    assert (await hw.trigger_periodic_increase())["started"] is True


async def test_trigger_restores_enable_bit_if_second_write_fails(tmp_path) -> None:
    hw, pump, *_ = make(tmp_path, {47050: 1})
    original = pump.write
    state = {"n": 0}

    async def flaky(register, value, **kw):
        state["n"] += 1
        if state["n"] == 2:
            from nibe_service_usb.pump.interface import PumpTimeout

            raise PumpTimeout("no reply")
        await original(register, value, **kw)

    pump.write = flaky  # type: ignore[method-assign]
    with pytest.raises(CommandError) as exc:
        await hw.trigger_periodic_increase()
    assert exc.value.code == "pump_timeout"
    assert pump.registers[47050] == 1  # restored


async def test_trigger_offline(tmp_path) -> None:
    hw, pump, *_ = make(tmp_path)
    pump.go_offline()
    with pytest.raises(CommandError) as exc:
        await hw.trigger_periodic_increase()
    assert exc.value.code == "pump_offline"


async def test_poller_hook_updates_periodic_status_from_sensors(tmp_path) -> None:
    hw, pump, state, rec, *_ = make(tmp_path, {47050: 1, 43014: 0, 43109: 3, 47041: 2, 40313: 0})
    pump.ram[PI_TIMER_ADDRESS] = bytes((2, 0, 0))
    await hw.poller.poll_now()
    pi = state.hot_water.periodic_increase
    assert (pi.running, pi.remaining_seconds, pi.hold, pi.enabled) == (True, 7200, False, True)
    assert state.hot_water.mode == "lux"
    assert state.hot_water.effective_mode == "periodic"
    assert state.hot_water.reason == "comfort"
    assert rec.of("status_changed")[-1]["hot_water"]["periodic_increase"]["running"] is True
    # run ended
    pump.ram[PI_TIMER_ADDRESS] = bytes(3)
    pump.registers[43014] = 1  # also 1 when idle: hold is only meaningful while running
    await hw.poller.poll_now()
    assert (pi.running, pi.remaining_seconds, pi.hold) == (False, 0, False)
