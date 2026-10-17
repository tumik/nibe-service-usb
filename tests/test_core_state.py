from __future__ import annotations

from test_core_fakes import FakeClock, make_state

from nibe_service_usb.pump.registers import load_sensor_defs

SENSORS = load_sensor_defs(None)


def test_snapshot_status_shape_matches_protocol_doc() -> None:
    state, _, _ = make_state()
    status = state.snapshot()["status"]
    assert list(status) == [
        "version",
        "uptime",
        "pump",
        "heating",
        "hot_water",
        "watchdog",
        "identity",
        "clients",
    ]
    assert set(status["pump"]) == {"online", "port", "last_ok", "errors"}
    assert set(status["heating"]) == {
        "mode",
        "active",
        "suspended_reason",
        "last_push",
        "next_push",
        "target_dm",
        "current_dm",
    }
    assert set(status["hot_water"]) == {"mode", "effective_mode", "reason", "periodic_increase"}
    assert set(status["hot_water"]["periodic_increase"]) == {
        "running",
        "remaining_seconds",
        "hold",
        "enabled",
    }
    assert set(status["watchdog"]) == {"ha_alive", "last_heartbeat", "timeout", "reverted_at"}
    assert set(status["identity"]) == {
        "product",
        "software_version",
        "base_version",
        "product_code",
        "clock_state",
        "clock_drift",
        "uptime",
        "last_restart",
        "eeprom_store_calls",
        "eeprom_write_failures",
        "eeprom_write_failed",
        "last_health_check",
    }
    assert status["version"] == "0.1.0"
    assert status["clients"] == 0
    assert status["heating"]["mode"] == "auto"


def test_uptime_is_server_process_uptime() -> None:
    mono = FakeClock(50.0)
    from nibe_service_usb.core.events import EventBus
    from nibe_service_usb.core.state import State

    state = State(SENSORS, EventBus(), "x", monotonic=mono)
    mono.advance(12.5)
    assert state.snapshot()["status"]["uptime"] == 12.5


def test_set_sensor_rounds_to_precision_before_comparing() -> None:
    state, _, _ = make_state()
    assert state.set_sensor("bt1_outdoor", -3.4, 1.0) is True
    assert state.set_sensor("bt1_outdoor", -3.44, 2.0) is False  # rounds to -3.4
    assert state.get("bt1_outdoor") == -3.4
    assert state.set_sensor("bt1_outdoor", -3.46, 3.0) is True
    assert state.get("bt1_outdoor") == -3.5
    # a timestamp refresh without change still updates the age
    assert state.sensor_snapshot(["bt1_outdoor"])["bt1_outdoor"]["ts"] == 3.0


def test_set_sensor_lists_and_enums() -> None:
    state, _, _ = make_state()
    assert state.set_sensor("active_alarms", [], 1.0) is True
    assert state.set_sensor("active_alarms", [], 2.0) is False
    assert state.set_sensor("active_alarms", [{"number": 1, "title": "x"}], 3.0) is True
    assert state.set_sensor("active_alarms", [{"number": 1, "title": "x"}], 4.0) is False
    assert state.set_sensor("op_priority", "heating", 1.0) is True
    assert state.set_sensor("op_priority", "heating", 2.0) is False
    assert state.set_sensor("op_priority", "off", 3.0) is True


def test_three_failures_make_value_none_and_report_change() -> None:
    state, _, _ = make_state()
    state.set_sensor("bt2_flow", 35.0, 1.0)
    assert state.sensor_failed("bt2_flow") is False
    assert state.sensor_failed("bt2_flow") is False
    assert state.get("bt2_flow") == 35.0
    assert state.sensor_failed("bt2_flow") is True
    assert state.get("bt2_flow") is None
    assert state.sensor_failed("bt2_flow") is False  # already None
    assert state.set_sensor("bt2_flow", 36.0, 2.0) is True  # recovery counts as a change


def test_success_resets_failure_counter() -> None:
    state, _, _ = make_state()
    state.set_sensor("bt2_flow", 35.0, 1.0)
    state.sensor_failed("bt2_flow")
    state.sensor_failed("bt2_flow")
    state.set_sensor("bt2_flow", 35.0, 2.0)
    assert state.sensor_failed("bt2_flow") is False
    assert state.get("bt2_flow") == 35.0


def test_sensor_age_uses_clock() -> None:
    clock = FakeClock(100.0)
    state, _, _ = make_state(clock=clock)
    assert state.sensor_age("bt1_outdoor") is None
    state.set_sensor("bt1_outdoor", 1.0, 100.0)
    clock.advance(30)
    assert state.sensor_age("bt1_outdoor") == 30


def test_sensor_snapshot_has_metadata_and_skips_disabled() -> None:
    state, _, _ = make_state()
    state.set_sensor("hw_comfort", "normal", 5.0)
    snap = state.sensor_snapshot()
    assert snap["hw_comfort"]["value"] == "normal"
    assert snap["hw_comfort"]["ts"] == 5.0
    assert snap["hw_comfort"]["options"] == ["eco", "normal", "lux"]
    assert snap["bt1_outdoor"]["unit"] == "°C"
    assert snap["bt1_outdoor"]["value"] is None
    assert "bt50_room" not in snap  # enabled: false
    assert set(state.sensor_snapshot(["hw_comfort", "nope"])) == {"hw_comfort"}
    assert state.snapshot()["sensors"].keys() == snap.keys()


async def test_status_and_sensor_events_are_published() -> None:
    state, _, rec = make_state()
    state.heating.mode = "disable"
    await state.status_changed()
    await state.sensors_changed({"bt1_outdoor": -3.4})
    await state.sensors_changed({})  # nothing to say
    assert rec.of("status_changed")[0]["heating"]["mode"] == "disable"
    assert rec.of("status_changed")[0] == state.snapshot()["status"] | {
        "uptime": rec.of("status_changed")[0]["uptime"]
    }
    assert rec.of("sensor_changed") == [{"values": {"bt1_outdoor": -3.4}}]
