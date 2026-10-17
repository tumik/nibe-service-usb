from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from test_core_fakes import FakeClock, FakePump, make_config, make_state, wait_for

from nibe_service_usb.core.poller import SLOW_CORE_SENSORS, SensorPoller, seconds_until
from nibe_service_usb.pump.interface import PumpTimeout
from nibe_service_usb.pump.registers import load_alarm_defs, load_sensor_defs

SENSORS = load_sensor_defs(None)
ALARMS = load_alarm_defs(None)


def make(tmp_path, pump=None, **sections):
    pump = pump or FakePump()
    config = make_config(tmp_path, **sections)
    clock = FakeClock(5000.0)
    state, bus, rec = make_state(clock=clock)
    poller = SensorPoller(config, pump, state, SENSORS, alarms=ALARMS, clock=clock)
    return poller, pump, state, rec, clock


def registers_of(pump: FakePump, *names: str) -> set[int]:
    by_name = {s.name: s.register for s in SENSORS}
    return {by_name[n] for n in names}


async def test_cycle_publishes_one_sensor_changed_with_decoded_values(tmp_path) -> None:
    poller, pump, state, rec, _ = make(tmp_path)
    await poller.poll_now()
    events = rec.of("sensor_changed")
    assert len(events) == 1
    values = events[0]["values"]
    assert values["bt1_outdoor"] == -3.4  # RT 1
    assert values["bt2_flow"] == 35.0
    assert values["bt17_suction"] == 10.0
    assert values["avg_outdoor"] == -2.0  # RI (no rt_field)
    assert values["calc_flow"] == 34.0
    assert values["hw_mode"] == "normal"
    assert values["hw_comfort"] == "normal"
    assert values["op_priority"] == "heating"
    assert values["heat_demand"] is True
    assert values["compressor_fsm"] == "idle"
    assert values["pi_running"] is False  # RM
    assert values["pi_remaining"] == 0
    assert values["active_alarms"] == []
    assert values["set_start_comp"] == -200  # settings tier, first cycle
    assert values["uptime"] == 1000
    assert "bt25_ext_flow" not in values  # disabled
    assert "bt50_room" not in values
    assert "eeprom_store_calls" not in values  # identity tier only at health check
    assert "comp_perm" not in values  # diagnostic off
    assert state.get("bt1_outdoor") == -3.4
    assert ("read_temperatures",) in pump.calls


async def test_unchanged_values_publish_nothing_and_precision_filters_jitter(tmp_path) -> None:
    poller, pump, state, rec, _ = make(tmp_path)
    await poller.poll_now()
    rec.clear()
    await poller.poll_now()
    assert rec.of("sensor_changed") == []
    pump.temperatures[0] = -35  # real change
    pump.registers[43009] = 341  # 34.1: a change at precision 1
    await poller.poll_now()
    assert rec.of("sensor_changed") == [{"values": {"bt1_outdoor": -3.5, "calc_flow": 34.1}}]


async def test_active_alarms_list_changes_when_set_changes(tmp_path) -> None:
    poller, pump, state, rec, _ = make(tmp_path)
    await poller.poll_now()
    rec.clear()
    pump.registers[45196] = 1  # alarm 50
    pump.registers[45045] = 1  # alarm 53
    await poller.poll_now()
    expected = [
        {"number": 50, "title": "High pressure alarm"},
        {"number": 53, "title": "Level sensor brine"},
    ]
    assert rec.of("sensor_changed") == [{"values": {"active_alarms": expected}}]
    rec.clear()
    await poller.poll_now()
    assert rec.of("sensor_changed") == []
    pump.registers[45196] = 0
    await poller.poll_now()
    assert rec.of("sensor_changed")[0]["values"]["active_alarms"] == expected[1:]
    # all flag registers are read in one read_many
    assert any(
        call[0] == "read_many" and set(a.register for a in ALARMS) <= set(call[1]) for call in pump.calls
    )


async def test_settings_tier_every_nth_cycle_and_diagnostic_opt_in(tmp_path) -> None:
    poller, pump, state, rec, _ = make(tmp_path, poll={"settings_every": 3})
    (settings_reg,) = registers_of(pump, "set_start_comp")
    seen = []
    for _ in range(4):
        pump.calls.clear()
        await poller.poll_now()
        seen.append(settings_reg in pump.registers_read())
    assert seen == [True, False, False, True]
    assert 43112 not in pump.registers_read()

    poller, pump, state, rec, _ = make(tmp_path, poll={"diagnostic": True})
    await poller.poll_now()
    assert 43112 in pump.registers_read()  # comp_perm


async def test_explicit_tiers(tmp_path) -> None:
    poller, pump, *_ = make(tmp_path)
    await poller.poll_now(tiers={"settings"})
    read = set(pump.registers_read())
    assert 47206 in read
    assert 40008 not in read
    assert ("read_temperatures",) not in pump.calls


async def test_failed_reads_become_none_after_three_cycles(tmp_path) -> None:
    poller, pump, state, rec, _ = make(tmp_path)
    await poller.poll_now()
    rec.clear()
    pump.fail_registers[43110] = PumpTimeout("no reply")  # fails the whole core register group
    for _ in range(2):
        await poller.poll_now()
        assert rec.of("sensor_changed") == []
    assert state.get("heat_demand") is True  # keeps old value
    await poller.poll_now()
    values = rec.of("sensor_changed")[0]["values"]
    assert values["heat_demand"] is None
    assert values["degree_minutes"] is None  # same group
    assert state.get("bt1_outdoor") == -3.4  # RT line unaffected


async def test_rt_failure_falls_back_to_ri(tmp_path) -> None:
    poller, pump, state, rec, _ = make(tmp_path)

    async def failing(**_kw):
        raise PumpTimeout("RT failed")

    pump.read_temperatures = failing  # type: ignore[method-assign]
    await poller.poll_now()
    assert state.get("bt1_outdoor") == -3.4
    assert 40004 in pump.registers_read()


async def test_offline_pump_is_skipped_quietly(tmp_path) -> None:
    poller, pump, state, rec, _ = make(tmp_path)
    pump.go_offline()
    await poller.poll_now()
    assert pump.calls == []
    assert rec.of("sensor_changed") == []
    # going offline in the middle of a cycle publishes what was read and stops
    pump2 = FakePump()
    poller2, pump2, state2, rec2, _ = make(tmp_path, pump2)
    original = pump2.read_many

    async def read_many_then_offline(registers, **kw):
        result = await original(registers, **kw)
        pump2.go_offline()
        return result

    pump2.read_many = read_many_then_offline  # type: ignore[method-assign]
    await poller2.poll_now()
    assert rec2.of("sensor_changed")[0]["values"]["bt1_outdoor"] == -3.4  # RT came first
    assert "active_alarms" not in rec2.of("sensor_changed")[0]["values"]


async def test_pump_status_is_synced(tmp_path) -> None:
    poller, pump, state, rec, _ = make(tmp_path)
    await poller.poll_now()
    assert state.pump.online is True
    assert state.pump.port == "/dev/fake"
    assert state.pump.last_ok == 1_760_000_000.0


async def test_ensure_fresh_polls_once(tmp_path) -> None:
    poller, pump, state, rec, _ = make(tmp_path)
    await poller.ensure_fresh()
    assert state.get("bt1_outdoor") == -3.4
    pump.calls.clear()
    await poller.ensure_fresh()
    assert pump.calls == []  # fresh


async def test_restart_detected_from_uptime_decrease(tmp_path) -> None:
    poller, pump, state, rec, clock = make(tmp_path)
    restarts: list[int] = []

    async def on_restart() -> None:
        restarts.append(1)

    poller.on_restart = on_restart
    await poller.poll_now()
    assert state.identity.uptime == 1000
    assert state.identity.last_restart is None
    pump.registers[43071] = 1600  # normal growth
    await poller.poll_now()
    assert restarts == []
    pump.calls.clear()
    pump.registers[43071] = 30  # display rebooted
    await poller.poll_now()
    assert restarts == [1]
    assert state.identity.last_restart == pytest.approx(5000.0 - 30)
    assert state.identity.uptime == 30
    assert ("identity",) in pump.calls  # full health check ran
    assert state.identity.last_health_check is not None


async def test_health_check_fills_identity(tmp_path) -> None:
    poller, pump, state, rec, clock = make(tmp_path)
    await poller.health_check()
    ident = state.identity
    assert ident.product == "F1226-6 R"
    assert ident.software_version == "7994R7"
    assert ident.base_version == 25
    assert ident.product_code == "06522820001001"
    assert ident.clock_state == "unset"  # display clock counts from 2010
    assert ident.eeprom_store_calls == 96000
    assert ident.eeprom_write_failures == 0
    assert ident.eeprom_write_failed is False
    assert ident.uptime == 1000
    assert ident.last_health_check == 5000.0
    assert rec.of("status_changed")  # published
    values = rec.of("sensor_changed")[0]["values"]
    assert values["eeprom_store_calls"] == 96000  # identity tier sensors
    assert values["set_start_comp"] == -200  # settings tier at health check
    assert values["base_version"] == 25


@pytest.mark.parametrize(
    ("local", "unixtime", "expected_state"),
    [
        (datetime(2026, 10, 12, 12, 0, 0), 1_760_000_050, "ok"),
        (datetime(2026, 10, 12, 12, 0, 0), 1_760_000_500, "drifting"),
    ],
)
async def test_health_check_clock_state(tmp_path, local, unixtime, expected_state) -> None:
    poller, pump, state, *_ = make(tmp_path)
    pump.clock_local = local
    pump.clock_unixtime = unixtime
    await poller.health_check()
    assert state.identity.clock_state == expected_state
    assert state.identity.clock_drift == unixtime - pump.host_unixtime


async def test_health_check_eeprom_failure_detection(tmp_path) -> None:
    poller, pump, state, *_ = make(tmp_path)
    await poller.health_check()
    assert state.identity.eeprom_write_failed is False
    pump.registers[40965] = 2  # persisted failures increased since last check
    await poller.health_check()
    assert state.identity.eeprom_write_failed is True
    assert state.identity.eeprom_write_failures == 2
    await poller.health_check()  # no further increase
    assert state.identity.eeprom_write_failed is False
    pump.registers[40967] = 1  # failures since boot
    await poller.health_check()
    assert state.identity.eeprom_write_failed is True


async def test_health_check_offline_does_nothing(tmp_path) -> None:
    poller, pump, state, *_ = make(tmp_path)
    pump.go_offline()
    await poller.health_check()
    assert state.identity.last_health_check is None


def test_seconds_until_daily_time() -> None:
    assert seconds_until(datetime(2026, 10, 12, 3, 0, 0), "03:30") == 1800
    assert seconds_until(datetime(2026, 10, 12, 3, 30, 0), "03:30") == 86400
    assert seconds_until(datetime(2026, 10, 12, 23, 0, 0), "03:30") == 4.5 * 3600


async def test_start_runs_health_check_and_first_poll_when_pump_comes_online(tmp_path) -> None:
    pump = FakePump()
    pump.go_offline()
    poller, pump, state, rec, _ = make(tmp_path, pump)
    await poller.start()
    try:
        await asyncio.sleep(0.05)
        assert pump.calls == []  # offline: nothing happens
        await pump.set_online(True)
        await wait_for(
            lambda: state.get("bt1_outdoor") is not None and state.identity.last_health_check is not None
        )
        assert rec.of("pump_state") == [{"online": True}]
        assert state.pump.online is True
    finally:
        await poller.stop()
    assert poller._tasks == []


async def test_fast_cycle_reads_fast_sensors_between_full_polls(tmp_path) -> None:
    poller, pump, state, rec, _ = make(tmp_path, poll={"fast_interval": 10.0, "interval": 3600.0})
    await poller.start()
    try:
        await wait_for(lambda: state.get("bt1_outdoor") is not None)
        pump.calls.clear()
        rec.clear()
        pump.registers[40940] = -120
        pump.temperatures[0] = -35
        poller._next_fast = 0.0  # make the fast cycle due now
        poller._wake.set()
        await wait_for(lambda: state.get("degree_minutes") == -12.0)
        assert state.get("bt1_outdoor") == -3.5
        read = set(pump.registers_read())
        fast_regs = {
            s.register
            for s in SENSORS
            if s.enabled
            and s.register
            and s.tier == "core"
            and s.name not in SLOW_CORE_SENSORS
            and s.ram is None
            and s.rt_field is None
        }
        slow_regs = {s.register for s in SENSORS if s.name in SLOW_CORE_SENSORS}
        assert fast_regs <= read
        assert not (slow_regs & read)  # counters only in the full cycle
        assert 47206 not in read  # settings tier only in the full cycle
        assert any(call[0] == "read_temperatures" for call in pump.calls)
        assert any(call[0] == "read_ram" for call in pump.calls)  # periodic increase timer
        assert any(  # alarm flags are part of the fast cycle
            call[0] == "read_many" and 45196 in call[1] for call in pump.calls
        )
    finally:
        await poller.stop()


async def test_fast_cycle_detects_restart_via_uptime(tmp_path) -> None:
    poller, pump, state, rec, _ = make(tmp_path, poll={"fast_interval": 10.0, "interval": 3600.0})
    await poller.start()
    try:
        await wait_for(lambda: state.get("uptime") is not None)
        pump.registers[43071] = 5  # the display rebooted
        poller._next_fast = 0.0
        poller._wake.set()
        await wait_for(lambda: state.identity.last_restart is not None)
    finally:
        await poller.stop()


async def test_post_poll_hooks_run(tmp_path) -> None:
    poller, *_ = make(tmp_path)
    calls = []
    poller.on_polled.append(lambda: calls.append(1))
    await poller.poll_now()
    assert calls == [1]
