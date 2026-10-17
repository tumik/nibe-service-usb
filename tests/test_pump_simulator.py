from __future__ import annotations

import asyncio
import inspect

import pytest

from nibe_service_usb.pump import codec
from nibe_service_usb.pump.interface import (
    Priority,
    PumpClient,
    PumpOffline,
    PumpTimeout,
)
from nibe_service_usb.pump.registers import load_sensor_defs
from nibe_service_usb.pump.simulator import DM_ADDRESS, PI_TIMER_ADDRESS, SimulatedPumpClient

SENSORS = load_sensor_defs(None)


@pytest.fixture
def sim() -> SimulatedPumpClient:
    return SimulatedPumpClient(SENSORS)


def test_protocol_conformance(sim: SimulatedPumpClient) -> None:
    members = [name for name in dir(PumpClient) if not name.startswith("_")]
    assert members
    for name in members:
        assert hasattr(sim, name), name
    for name in members:
        proto_attr = inspect.getattr_static(PumpClient, name)
        if inspect.iscoroutinefunction(proto_attr):
            assert inspect.iscoroutinefunction(getattr(sim, name)), name
    assert inspect.isfunction(inspect.getattr_static(SimulatedPumpClient, "advance"))
    assert not inspect.iscoroutinefunction(sim.advance)


async def test_seed_values(sim: SimulatedPumpClient) -> None:
    await sim.start()
    assert sim.online
    assert await sim.read(40004) == 47
    values = await sim.read_many([40008, 40012, 40014, 40013, 47206, 47375, 43071, 40969, 99999])
    assert values == [262, 227, 480, 529, -200, 180, 82791, 96859, 0]
    # every register named in sensors.yaml is readable (0 unless seeded)
    regs = [s.register for s in SENSORS if s.register is not None]
    assert len(await sim.read_many(regs)) == len(regs)
    await sim.stop()


async def test_read_many_any_length(sim: SimulatedPumpClient) -> None:
    regs = list(range(40000, 40100))
    assert await sim.read_many(regs) == [sim.registers.get(r, 0) for r in regs]


async def test_temperatures_follow_registers(sim: SimulatedPumpClient) -> None:
    rt = await sim.read_temperatures()
    assert len(rt) == 28
    assert rt[0] == 47 and rt[1] == 262 and rt[2] == 227 and rt[5] == 480 and rt[6] == 529
    assert rt[12] == rt[2] and rt[15] == rt[13]  # copies of BT3 / BT14
    assert rt[17] == rt[18] == rt[19] == rt[20] == rt[24] == -32768
    sim.set_register(40004, -35)
    assert (await sim.read_temperatures())[0] == -35


async def test_identity_and_clock(sim: SimulatedPumpClient) -> None:
    ident = await sim.identity()
    assert (ident.product, ident.software_version, ident.brand) == ("F1226-6 R", "7994R7", 1)
    assert (ident.software_id, ident.base_version) == (7994, 25)
    assert ident.product_code == "06522820001001" and ident.article == "065228" and ident.serial == "001"
    assert ident.manufactured is not None and ident.manufactured.isoformat() == "2020-01-01"
    clock = await sim.clock()
    assert clock.local.year == 2010 and clock.state(120.0) == "unset"
    assert (clock.tz, clock.w_offset, clock.s_offset, clock.dls) == ("Helsinki", 2, 3, 0)
    sim.advance(3600)
    assert (await sim.clock()).unixtime == clock.unixtime + 3600


# -- writes -----


async def test_wi_counts_eeprom_wm_does_not(sim: SimulatedPumpClient) -> None:
    start = await sim.read(40969)
    await sim.write(40940, -20)
    assert await sim.read(40940) == -20
    assert await sim.read(40969) == start + 1
    for value in (-256, 0, -2000):
        await sim.write_ram(DM_ADDRESS, codec.encode_s32_le(value))
        assert await sim.read(40940) == value
        assert await sim.read_ram(DM_ADDRESS, 4) == codec.encode_s32_le(value)
    assert await sim.read(40969) == start + 1  # three WM writes: unchanged
    await sim.write(40940, -21)
    await sim.write(47041, 2)
    assert await sim.read(40969) == start + 3


async def test_write_allowlist(sim: SimulatedPumpClient) -> None:
    for register, value in ((40004, 1), (47041, 3), (40940, 5)):
        with pytest.raises(ValueError):
            await sim.write(register, value)
    with pytest.raises(ValueError):
        await sim.write_ram(0x20000000, bytes(4))
    with pytest.raises(ValueError):
        await sim.write_ram(DM_ADDRESS, bytes(2))


async def test_hot_water_mode_follows_comfort_setting(sim: SimulatedPumpClient) -> None:
    await sim.write(47041, 2)
    assert await sim.read_many([47041, 43109]) == [2, 2]


# -- periodic increase -----


async def test_periodic_increase_trigger(sim: SimulatedPumpClient) -> None:
    assert await sim.read_ram(PI_TIMER_ADDRESS, 3) == b"\x00\x00\x00"
    await sim.write(47050, 0)
    sim.advance(5)
    await sim.write(47050, 1)
    assert await sim.read_ram(PI_TIMER_ADDRESS, 3) == bytes((5, 0, 0))
    assert await sim.read_many([43109, 40313, 43014, 47050]) == [3, 3, 0, 1]
    sim.advance(3600 + 30 * 60 + 15)
    assert await sim.read_ram(PI_TIMER_ADDRESS, 3) == bytes((3, 29, 45))
    sim.advance(3 * 3600 + 29 * 60 + 45)
    assert await sim.read_ram(PI_TIMER_ADDRESS, 3) == b"\x00\x00\x00"
    assert await sim.read_many([43109, 40313, 43014]) == [1, 0, 1]


async def test_periodic_increase_needs_the_pair_within_30_seconds(sim: SimulatedPumpClient) -> None:
    await sim.write(47050, 0)
    sim.advance(31)
    await sim.write(47050, 1)
    assert await sim.read_ram(PI_TIMER_ADDRESS, 3) == b"\x00\x00\x00"
    assert await sim.read(43109) == 1
    # a lone write of 1 does nothing either
    await sim.write(47050, 1)
    assert await sim.read_ram(PI_TIMER_ADDRESS, 3) == b"\x00\x00\x00"


# -- degree minutes and the compressor -----


async def test_dm_integrates_when_heat_demand(sim: SimulatedPumpClient) -> None:
    sim.set_register(43009, 300)  # calculated flow 30.0, BT2 = 26.2 -> -38 raw per minute
    sim.set_register(43110, 1)
    sim.advance(59)
    assert sim.registers[40940] == 0
    sim.advance(1)
    assert sim.registers[40940] == -38
    sim.advance(10 * 60)
    assert sim.registers[40940] == -38 * 11
    sim.set_register(43110, 0)
    sim.advance(60)
    assert sim.registers[40940] == 0  # the pump resets DM without heat demand


async def test_dm_write_is_reset_without_heat_demand(sim: SimulatedPumpClient) -> None:
    await sim.write_ram(DM_ADDRESS, codec.encode_s32_le(-22))
    assert await sim.read(40940) == -22
    sim.advance(60)
    assert await sim.read(40940) == 0


async def test_dm_integration_is_independent_of_step_size() -> None:
    a, b = SimulatedPumpClient(SENSORS), SimulatedPumpClient(SENSORS)
    for s in (a, b):
        s.set_register(43009, 290)
        s.set_register(43110, 1)
    a.advance(1800)
    for _ in range(1800):
        b.advance(1)
    assert a.registers[40940] == b.registers[40940] == (262 - 290) * 30
    assert a.registers[43071] == b.registers[43071] == 82791 + 1800


async def test_compressor_fsm_start_and_stop(sim: SimulatedPumpClient) -> None:
    sim.set_register(43009, 400)  # flow far below target: -138 raw per minute
    sim.set_register(43110, 1)
    states = []
    for _ in range(40):
        sim.advance(60)
        states.append(
            (sim.registers[40940], sim.registers[43427], sim.registers[43435], sim.registers[43443])
        )
    # threshold 47206 * 10 = -2000: reached after 15 minutes (15 * -138 = -2070)
    first_start = next(i for i, s in enumerate(states) if s[1] == 40)
    assert states[first_start][0] < -2000 and states[first_start - 1][0] >= -2000 or first_start == 0
    assert [s[1] for s in states[first_start : first_start + 2]] == [40, 60]
    assert states[first_start + 1][2:] == (1, 6)  # relay command on, PCA base status running
    assert sim.registers[43416] == 6  # compressor start counted
    # heat demand drops: DM resets to 0 (>= 0) and the compressor stops 60 -> 100 -> 20
    sim.set_register(43110, 0)
    sim.advance(60)
    assert sim.registers[43427] == 100
    sim.advance(60)
    assert sim.registers[43427] == 20
    assert (sim.registers[43435], sim.registers[43443]) == (0, 5)


async def test_compressor_stays_idle_above_threshold(sim: SimulatedPumpClient) -> None:
    sim.set_register(43009, 270)  # -8 raw per minute
    sim.set_register(43110, 1)
    sim.advance(60 * 100)  # 100 minutes -> -800 > -2000
    assert sim.registers[43427] == 20


async def test_compressor_hours_count_while_running(sim: SimulatedPumpClient) -> None:
    sim.set_register(43427, 60)
    sim.set_register(43110, 1)
    sim.set_register(40940, -500)
    sim.set_register(43009, 262)
    sim.advance(2 * 3600)
    assert sim.registers[43420] == 12


# -- unplug / plug -----


async def test_unplug_and_plug(sim: SimulatedPumpClient) -> None:
    await sim.start()
    sync_events: list[bool] = []
    async_events: list[bool] = []

    async def acb(online: bool) -> None:
        async_events.append(online)

    sim.on_state_change(sync_events.append)
    sim.on_state_change(acb)
    sim.unplug()
    assert not sim.online
    sim.unplug()  # idempotent, no second event
    for call in (
        sim.read(40004),
        sim.read_many([40004]),
        sim.read_temperatures(),
        sim.read_ram(DM_ADDRESS, 4),
        sim.write(47041, 1),
        sim.write_ram(DM_ADDRESS, bytes(4)),
        sim.identity(),
        sim.clock(),
    ):
        with pytest.raises(PumpOffline):
            await call
    sim.plug()
    await asyncio.sleep(0)
    assert sim.online
    assert await sim.read(40004) == 47
    assert sync_events == [False, True]
    assert async_events == [False, True]
    await sim.stop()


async def test_time_keeps_running_while_unplugged(sim: SimulatedPumpClient) -> None:
    sim.unplug()
    sim.advance(100)
    sim.plug()
    assert await sim.read(43071) == 82891


# -- latency and failure injection -----


async def test_fail_rate_injects_timeouts() -> None:
    sim = SimulatedPumpClient(SENSORS, fail_rate=1.0)
    with pytest.raises(PumpTimeout):
        await sim.read(40004)
    assert sim.error_count == 1
    assert sim.online  # a timeout is not an outage
    sim.fail_rate = 0.0
    assert await sim.read(40004) == 47
    assert sim.last_ok is not None

    flaky = SimulatedPumpClient(SENSORS, fail_rate=0.5, seed=1)
    outcomes = []
    for _ in range(200):
        try:
            await flaky.read(40004)
            outcomes.append(True)
        except PumpTimeout:
            outcomes.append(False)
    assert 60 < outcomes.count(False) < 140


async def test_latency_sleeps() -> None:
    sim = SimulatedPumpClient(SENSORS, latency=0.05)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await sim.read(40004, priority=Priority.COMMAND)
    assert loop.time() - t0 >= 0.04


async def test_realtime_ticker_advances_time() -> None:
    sim = SimulatedPumpClient(SENSORS, realtime=True)
    await sim.start()
    await asyncio.sleep(1.2)
    await sim.stop()
    assert sim.registers[43071] >= 82791 + 1
