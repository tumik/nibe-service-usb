from __future__ import annotations

import asyncio

import pytest
from test_core_fakes import DM_ADDRESS, FakeClock, FakePump, make_config, make_state, wait_for

from nibe_service_usb.core.errors import CommandError
from nibe_service_usb.core.heating import HeatingController
from nibe_service_usb.pump.interface import PumpTimeout
from nibe_service_usb.pump.registers import load_sensor_defs

SENSORS = load_sensor_defs(None)


def dm_bytes(value: int) -> bytes:
    return value.to_bytes(4, "little", signed=True)


def make(tmp_path, registers=None, pump=None, **sections):
    pump = pump or FakePump(registers)
    config = make_config(tmp_path, **sections)
    clock = FakeClock(10_000.0)
    state, bus, rec = make_state(clock=clock)
    heating = HeatingController(config, pump, state, clock=clock)
    return heating, pump, state, rec, clock


# (mode, registers, expected written value or None, expected suspended_reason)
# Defaults: R47206 = -200 -> threshold -2000, enable target -1999, disable value -1.
TABLE = [
    pytest.param("disable", {40940: -15}, -1, None, id="disable-dm-below-disable-value"),
    pytest.param("disable", {40940: -1}, -1, None, id="disable-dm-equals-disable-value"),
    pytest.param("disable", {40940: 20}, None, None, id="disable-dm-above-nothing-to-gain"),
    pytest.param("disable", {40940: 0}, None, "dm_zero", id="disable-dm-zero"),
    pytest.param("disable", {40940: -2500}, -1, None, id="disable-compressor-running-deep-dm"),
    pytest.param("disable", {40940: -15, 43427: 60}, -1, None, id="disable-fsm-running-still-writes"),
    pytest.param("disable", {40940: -15, 43110: 0}, None, None, id="disable-heat-demand-zero"),
    pytest.param("disable", {40940: -15, 43086: 20}, None, None, id="disable-hot-water-priority"),
    pytest.param("disable", {40940: 0, 43110: 1}, None, "dm_zero", id="disable-dm-exactly-zero"),
    pytest.param("enable", {40940: 0, 43110: 1}, None, "dm_zero", id="enable-dm-exactly-zero"),
    pytest.param("disable", {40940: -15, 40008: 150}, None, "safety_supply_temp", id="disable-supply-guard"),
    pytest.param(
        "disable", {40940: -15, 40004: -160}, None, "safety_outdoor_temp", id="disable-outdoor-guard"
    ),
    pytest.param("disable", {40940: -15, 40008: -32768}, None, "stale_data", id="disable-open-supply-sensor"),
    pytest.param("enable", {40940: -100}, -1999, None, id="enable-dm-above-target"),
    pytest.param("enable", {40940: -1999}, None, None, id="enable-dm-equals-target"),
    pytest.param("enable", {40940: -2500}, None, None, id="enable-dm-below-target"),
    pytest.param("enable", {40940: -100, 43427: 60}, -1999, None, id="enable-running-still-writes"),
    pytest.param("enable", {40940: -2500, 43427: 60}, None, None, id="enable-running-dm-below-target"),
    pytest.param("enable", {40940: -100, 43405: 7}, -1999, None, id="enable-start-inhibit-still-writes"),
    pytest.param("enable", {40940: -100, 43110: 0}, None, None, id="enable-heat-demand-zero"),
    pytest.param("enable", {40940: -100, 43086: 20}, None, None, id="enable-hot-water-priority"),
    pytest.param(
        "enable", {40940: -100, 43427: 40}, -1999, None, id="enable-compressor-starting-still-writes"
    ),
    pytest.param("enable", {40940: -100, 47206: -60}, -599, None, id="enable-default-threshold-minus-60"),
    pytest.param("enable", {40940: -100, 40008: 150}, -1999, None, id="enable-has-no-guards"),
    pytest.param("auto", {40940: -100}, None, None, id="auto-never-writes"),
]


@pytest.mark.parametrize(("mode", "registers", "written", "reason"), TABLE)
async def test_decision_table(tmp_path, mode, registers, written, reason) -> None:
    heating, pump, state, rec, _ = make(tmp_path, registers)
    result = await heating.set_mode(mode)
    assert result["written_dm"] == written
    assert result["mode"] == mode
    assert result["suspended_reason"] == reason
    assert result["active"] is (mode != "auto" and reason is None)
    if written is None:
        assert pump.writes == []
    else:
        assert pump.writes == [("ram", DM_ADDRESS, dm_bytes(written))]
        assert result["target_dm"] == written
        assert result["current_dm"] == written  # from the read-back
        assert state.heating.last_push == 10_000.0
    await heating.stop()


async def test_start_threshold_override_in_config(tmp_path) -> None:
    heating, pump, *_ = make(
        tmp_path, {40940: -100}, heating={"dm_start_threshold": -600, "dm_enable_margin": 3}
    )
    result = await heating.set_mode("enable")
    assert result["written_dm"] == -597  # config threshold, not R47206
    await heating.stop()


async def test_threshold_is_read_at_every_evaluation(tmp_path) -> None:
    heating, pump, *_ = make(tmp_path, {40940: -100}, heating={"dm_push_interval": 0.05})
    await heating.set_mode("enable")
    pump.registers[40940] = -100
    pump.registers[47206] = -300  # user changed the setting on the panel
    await wait_for(lambda: ("ram", DM_ADDRESS, dm_bytes(-2999)) in pump.writes)
    await heating.stop()


async def test_written_value_is_clamped_to_configured_bounds(tmp_path) -> None:
    heating, pump, *_ = make(tmp_path, {40940: -100, 47206: -2000}, heating={"dm_min": -5000})
    result = await heating.set_mode("enable")  # -20000 + 1 clamped to -5000
    assert result["written_dm"] == -5000
    await heating.stop()


async def test_current_dm_and_sensor_updated(tmp_path) -> None:
    heating, pump, state, rec, _ = make(tmp_path, {40940: -15})
    await heating.set_mode("disable")
    assert state.heating.current_dm == -1
    assert state.get("degree_minutes") == -0.1
    assert state.get("heat_demand") is True
    assert {"degree_minutes": -0.1}.items() <= rec.of("sensor_changed")[-1]["values"].items()
    await heating.stop()


# ----------------------------------------------------------------- guards with hysteresis


async def test_supply_guard_hysteresis_across_pushes(tmp_path) -> None:
    heating, pump, state, rec, _ = make(
        tmp_path, {40940: -15, 40008: 190}, heating={"dm_push_interval": 3600}
    )
    result = await heating.set_mode("disable")
    assert result["suspended_reason"] == "safety_supply_temp"
    assert result["active"] is False
    assert pump.writes == []
    # back at 20.5: above the limit but not above limit + hysteresis -> still suspended
    pump.registers[40008] = 205
    async with heating._lock:
        await heating._evaluate()
    assert state.heating.suspended_reason == "safety_supply_temp"
    assert pump.writes == []
    pump.registers[40008] = 215
    async with heating._lock:
        written = await heating._evaluate()
    assert written == -1
    assert state.heating.suspended_reason is None
    assert state.heating.active is True
    await heating.stop()


async def test_room_guard_reads_bt50_even_though_sensor_is_disabled(tmp_path) -> None:
    heating, pump, state, *_ = make(tmp_path, {40940: -15, 40033: 170}, safety={"min_room_temp": 18.0})
    result = await heating.set_mode("disable")
    assert result["suspended_reason"] == "safety_room_temp"
    assert 40033 in pump.registers_read()
    await heating.stop()


async def test_guard_sensors_are_refreshed_with_control_read_before_a_push(tmp_path) -> None:
    heating, pump, state, *_ = make(tmp_path, {40940: -15})
    await heating.set_mode("disable")
    assert [c for c in pump.calls if c[0] == "read_many" and 40008 in c[1] and 40004 in c[1]]
    assert state.get("bt2_flow") == 35.0
    await heating.stop()


async def test_suspend_and_resume_publish_status_only_on_transitions(tmp_path) -> None:
    heating, pump, state, rec, _ = make(tmp_path, {40940: -15}, heating={"dm_push_interval": 3600})
    await heating.set_mode("disable")
    assert len(rec.of("status_changed")) == 1
    async with heating._lock:
        await heating._evaluate()  # nothing changed
    assert len(rec.of("status_changed")) == 1
    pump.registers[40008] = 100
    async with heating._lock:
        await heating._evaluate()
    assert rec.of("status_changed")[-1]["heating"]["suspended_reason"] == "safety_supply_temp"
    assert len(rec.of("status_changed")) == 2
    await heating.stop()


# ----------------------------------------------------------------- write methods


async def test_register_method_uses_wi_and_daily_cap(tmp_path) -> None:
    heating, pump, state, *_ = make(
        tmp_path,
        {40940: -15},
        heating={"dm_write_method": "register", "max_writes_per_day": 2, "dm_push_interval": 3600},
    )
    result = await heating.set_mode("disable")
    assert result["written_dm"] == -1
    assert pump.writes == [("reg", 40940, -1)]
    for _ in range(2):
        pump.registers[40940] = -15
        async with heating._lock:
            await heating._evaluate()
    assert pump.writes == [("reg", 40940, -1), ("reg", 40940, -1)]
    assert state.heating.suspended_reason == "write_budget"
    assert state.heating.active is False
    await heating.stop()


async def test_register_budget_recovers_after_a_day(tmp_path) -> None:
    heating, pump, state, rec, clock = make(
        tmp_path,
        {40940: -15},
        heating={"dm_write_method": "register", "max_writes_per_day": 1, "dm_push_interval": 3600},
    )
    await heating.set_mode("disable")
    pump.registers[40940] = -15
    async with heating._lock:
        await heating._evaluate()
    assert state.heating.suspended_reason == "write_budget"
    clock.advance(86401)
    pump.registers[40940] = -15
    async with heating._lock:
        assert await heating._evaluate() == -1
    assert state.heating.suspended_reason is None
    await heating.stop()


async def test_ram_method_does_not_apply_the_cap(tmp_path) -> None:
    heating, pump, state, *_ = make(
        tmp_path, {40940: -15}, heating={"max_writes_per_day": 1, "dm_push_interval": 3600}
    )
    await heating.set_mode("disable")
    for _ in range(3):
        pump.registers[40940] = -15
        async with heating._lock:
            await heating._evaluate()
    assert len(pump.writes) == 4
    assert state.heating.suspended_reason is None
    await heating.stop()


async def test_threshold_policy_writes_only_near_the_start_threshold(tmp_path) -> None:
    heating, pump, *_ = make(tmp_path, {40940: -10}, heating={"dm_write_policy": "threshold"})
    assert (await heating.set_mode("disable"))["written_dm"] is None  # -10 is far from -2000 * 0.5
    pump.registers[40940] = -1200
    assert (await heating.set_mode("disable"))["written_dm"] == -1
    await heating.stop()


async def test_write_failure_is_reported_and_retried_next_time(tmp_path) -> None:
    heating, pump, state, *_ = make(tmp_path, {40940: -15})
    pump.fail_writes = True
    result = await heating.set_mode("disable")
    assert result["written_dm"] is None
    assert result["active"] is False
    assert result["suspended_reason"] == "write_failed"
    pump.fail_writes = False
    result = await heating.set_mode("disable")
    assert result["written_dm"] == -1
    assert result["suspended_reason"] is None
    await heating.stop()


# ----------------------------------------------------------------- RAM address verification


async def test_ram_address_verified_once(tmp_path) -> None:
    heating, pump, state, *_ = make(tmp_path, {40940: -15})
    await heating.start()
    reads = [c for c in pump.calls if c[0] == "read_ram"]
    assert len(reads) == 1
    await heating.set_mode("disable")
    await heating.set_mode("disable")
    assert len([c for c in pump.calls if c[0] == "read_ram"]) == 1
    await heating.stop()


async def test_wrong_ram_address_blocks_all_writes(tmp_path) -> None:
    pump = FakePump({40940: -15})
    pump.dm_address = 0x20000000  # the configured address does not hold DM
    heating, pump, state, rec, _ = make(tmp_path, pump=pump)
    result = await heating.set_mode("disable")
    assert result["suspended_reason"] == "dm_address_invalid"
    assert result["active"] is False
    assert result["written_dm"] is None
    result = await heating.set_mode("enable")  # sticky
    assert result["suspended_reason"] == "dm_address_invalid"
    assert pump.writes == []
    assert rec.of("status_changed")
    await heating.stop()


async def test_register_method_does_not_need_the_ram_address(tmp_path) -> None:
    pump = FakePump({40940: -15})
    pump.dm_address = 0x20000000
    heating, pump, *_ = make(tmp_path, pump=pump, heating={"dm_write_method": "register"})
    result = await heating.set_mode("disable")
    assert result["written_dm"] == -1
    assert not [c for c in pump.calls if c[0] == "read_ram"]
    await heating.stop()


# ----------------------------------------------------------------- pump offline


async def test_pump_offline_stores_mode_and_defers(tmp_path) -> None:
    heating, pump, state, rec, _ = make(tmp_path, {40940: -15})
    pump.go_offline()
    result = await heating.set_mode("disable")
    assert result["mode"] == "disable"
    assert result["active"] is False
    assert result["suspended_reason"] == "pump_offline"
    assert result["written_dm"] is None
    assert pump.writes == []
    assert rec.of("status_changed")[-1]["heating"]["suspended_reason"] == "pump_offline"

    await pump.set_online(True)
    await heating.on_pump_online()
    assert pump.writes == [("ram", DM_ADDRESS, dm_bytes(-1))]
    assert state.heating.active is True
    assert state.heating.suspended_reason is None
    await heating.stop()


async def test_pump_timeout_during_evaluation_is_pump_offline(tmp_path) -> None:
    heating, pump, state, *_ = make(tmp_path, {40940: -15})
    pump.fail_registers[43110] = PumpTimeout("no reply")
    result = await heating.set_mode("disable")
    assert result["suspended_reason"] == "pump_offline"
    assert pump.writes == []
    pump.fail_registers.clear()
    await heating.on_pump_online()
    assert state.heating.active is True
    await heating.stop()


async def test_on_pump_online_in_auto_does_nothing(tmp_path) -> None:
    heating, pump, *_ = make(tmp_path)
    await heating.on_pump_online()
    assert pump.calls == []


# ----------------------------------------------------------------- modes and timer


async def test_invalid_mode(tmp_path) -> None:
    heating, pump, *_ = make(tmp_path)
    with pytest.raises(CommandError) as exc:
        await heating.set_mode("turbo")
    assert exc.value.code == "invalid_params"


async def test_status_fields_and_next_push(tmp_path) -> None:
    heating, pump, state, rec, clock = make(tmp_path, {40940: -15}, heating={"dm_push_interval": 600})
    await heating.set_mode("disable")
    assert state.heating.last_push == 10_000.0
    assert state.heating.next_push == 10_600.0
    assert state.heating.target_dm == -1
    await heating.set_mode("auto")
    assert state.heating.next_push is None
    assert state.heating.target_dm is None
    assert state.heating.active is False
    await heating.stop()


async def test_timer_pushes_every_interval_and_auto_cancels_it(tmp_path) -> None:
    heating, pump, state, *_ = make(tmp_path, {40940: -15}, heating={"dm_push_interval": 0.05})
    await heating.set_mode("disable")
    await wait_for(lambda: len(pump.writes) >= 3)
    await heating.set_mode("auto")
    count = len(pump.writes)
    await asyncio.sleep(0.2)
    assert len(pump.writes) == count
    assert heating._task is None
    await heating.stop()


async def test_set_mode_auto_writes_nothing(tmp_path) -> None:
    heating, pump, *_ = make(tmp_path, {40940: -15})
    await heating.set_mode("disable")
    pump.writes.clear()
    result = await heating.set_mode("auto")
    assert result["written_dm"] is None
    assert result["mode"] == "auto"
    assert pump.writes == []
    await heating.stop()


async def test_stop_cancels_timer_without_writing(tmp_path) -> None:
    heating, pump, *_ = make(tmp_path, {40940: -15}, heating={"dm_push_interval": 0.05})
    await heating.set_mode("disable")
    await heating.stop()
    count = len(pump.writes)
    await asyncio.sleep(0.15)
    assert len(pump.writes) == count
