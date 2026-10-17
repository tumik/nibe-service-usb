from __future__ import annotations

from pathlib import Path

import pytest

from nibe_service_usb.pump.registers import (
    SensorDef,
    SensorDefError,
    decode,
    load_alarm_defs,
    load_sensor_defs,
    metadata,
)

SENSORS = {s.name: s for s in load_sensor_defs(None)}


def test_packaged_file_loads() -> None:
    sensors = load_sensor_defs(None)
    names = [s.name for s in sensors]
    assert len(names) == len(set(names))
    assert len(sensors) > 80
    assert {"core", "settings", "identity", "diagnostic"} == {s.tier for s in sensors}


def test_expected_sensors_present() -> None:
    for name in (
        "bt1_outdoor",
        "degree_minutes",
        "hw_comfort",
        "pi_running",
        "pi_remaining",
        "active_alarms",
        "set_start_comp",
        "uptime",
        "eeprom_store_calls",
        "eeprom_write_failures",
        "eeprom_write_failures_boot",
        "country",
        "base_version",
        "control_flow",
        "tb_input",
    ):
        assert name in SENSORS, name
    assert SENSORS["uptime"].register == 43071
    assert SENSORS["country"].register == 48745


def test_registers_unique_per_source() -> None:
    registers = [s.register for s in SENSORS.values() if s.register is not None]
    assert len(registers) == len(set(registers))


def test_temperatures_have_rt_field_and_register() -> None:
    bt1 = SENSORS["bt1_outdoor"]
    assert (bt1.register, bt1.rt_field, bt1.scale, bt1.type) == (40004, 1, 0.1, "s16")
    assert SENSORS["bt17_suction"].rt_field == 17
    assert SENSORS["avg_outdoor"].rt_field is None
    assert SENSORS["avg_outdoor"].register == 40067


def test_disabled_sensors() -> None:
    disabled = {s.name for s in SENSORS.values() if not s.enabled}
    assert disabled == {"bt25_ext_flow", "bt50_room"}
    assert (SENSORS["bt25_ext_flow"].register, SENSORS["bt25_ext_flow"].rt_field) == (40071, 25)
    assert SENSORS["bt50_room"].register == 40033


def test_degree_minutes_definition() -> None:
    dm = SENSORS["degree_minutes"]
    assert (dm.register, dm.type, dm.scale, dm.precision) == (40940, "s32", 0.1, 1)
    assert decode(dm, -20) == -2.0
    assert decode(dm, -22) == -2.2
    assert decode(dm, 0) == 0.0


def test_temperature_decode_and_sentinels() -> None:
    bt = SENSORS["bt2_flow"]
    assert decode(bt, 254) == 25.4
    assert decode(bt, -35) == -3.5
    assert decode(bt, 485) == 48.5
    assert decode(bt, -32768) is None
    assert decode(bt, -32767) is None
    assert decode(bt, 0) == 0.0


def test_enum_decode() -> None:
    mode = SENSORS["hw_comfort"]
    assert [decode(mode, i) for i in (0, 1, 2)] == ["eco", "normal", "lux"]
    assert decode(mode, 9) == "unknown_9"
    prio = SENSORS["op_priority"]
    assert decode(prio, 10) == "off"  # not the YAML boolean
    assert decode(SENSORS["hw_mode"], -1) == "off"
    assert decode(SENSORS["hw_mode"], 3) == "periodic"


def test_binary_decode() -> None:
    comp = SENSORS["compressor_running"]
    assert decode(comp, 0) is False
    assert decode(comp, 1) is True


def test_integer_scale_one_stays_int() -> None:
    value = decode(SENSORS["set_start_comp"], -200)
    assert value == -200
    assert isinstance(value, int)
    assert decode(SENSORS["uptime"], 82791) == 82791
    assert decode(SENSORS["comp_starts"], 1234) == 1234


def test_kw_scale() -> None:
    assert decode(SENSORS["el_power_kw"], 250) == 2.5
    assert decode(SENSORS["set_el_max"], 600) == 6.0


def test_ram_timer() -> None:
    running = SENSORS["pi_running"]
    remaining = SENSORS["pi_remaining"]
    assert running.ram == (0x2066718C, 3)
    assert remaining.ram == (0x2066718C, 3)
    assert running.register is None
    assert decode(running, bytes([0, 0, 0])) is False
    assert decode(running, bytes([4, 59, 30])) is True
    assert decode(running, bytes([0, 0, 1])) is True
    assert decode(remaining, bytes([0, 0, 0])) == 0
    assert decode(remaining, bytes([4, 59, 30])) == 4 * 3600 + 59 * 60 + 30
    with pytest.raises(TypeError):
        decode(remaining, 5)
    with pytest.raises(ValueError):
        decode(remaining, bytes([1, 2]))


def test_unsigned_and_signed_wrap() -> None:
    uptime = SENSORS["uptime"]
    assert decode(uptime, -1) == 0xFFFFFFFF
    s16 = SENSORS["set_curve"]
    assert decode(s16, 65535) == -1
    assert decode(s16, 1000) == 1000


def test_decode_rejects_alarm_list_and_bad_input() -> None:
    with pytest.raises(ValueError):
        decode(SENSORS["active_alarms"], 0)
    with pytest.raises(TypeError):
        decode(SENSORS["bt1_outdoor"], b"\x01")


def test_active_alarms_definition() -> None:
    sensor = SENSORS["active_alarms"]
    assert sensor.kind == "alarm_list"
    assert sensor.register is None and sensor.ram is None


def test_metadata_shape() -> None:
    meta = metadata(SENSORS["bt1_outdoor"])
    assert meta == {
        "name": "Outdoor temperature (BT1)",
        "kind": "temperature",
        "unit": "°C",
        "precision": 1,
        "tier": "core",
        "device_class": "temperature",
        "state_class": "measurement",
        "description": None,  # unset fields are null on the wire, never ""
    }
    enum_meta = metadata(SENSORS["hw_comfort"])
    assert enum_meta["options"] == ["eco", "normal", "lux"]
    assert enum_meta["kind"] == "enum"
    assert enum_meta["device_class"] == "enum"
    assert enum_meta["precision"] is None and enum_meta["unit"] is None
    assert "options" not in metadata(SENSORS["compressor_running"])
    assert metadata(SENSORS["active_alarms"])["kind"] == "alarm_list"


def test_alarm_defs() -> None:
    alarms = load_alarm_defs(None)
    assert len(alarms) == 53
    by_number = {a.number: a for a in alarms}
    assert by_number[1].register == 45005 and by_number[1].title == "Sensor fault BT1"
    assert by_number[68].title == "Pressure switch alarm (stage 2)"
    assert by_number[999].register == 43236
    assert len({a.register for a in alarms}) == 53


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "s.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_custom_file_and_bare_list(tmp_path: Path) -> None:
    path = write(tmp_path, "- name: a\n  label: A\n  register: 1\n")
    sensors = load_sensor_defs(path)
    assert isinstance(sensors[0], SensorDef)
    assert sensors[0].scale == 1.0 and sensors[0].tier == "core" and sensors[0].enabled
    assert load_alarm_defs(path) == []


@pytest.mark.parametrize(
    "body",
    [
        "- name: a\n  label: A\n  register: 1\n  bogus: 2\n",
        "- name: a\n  label: A\n",
        "- name: a\n  label: A\n  register: 1\n  type: float\n",
        "- name: a\n  label: A\n  register: 1\n  kind: enum\n",
        "- name: a\n  label: A\n  register: 1\n  tier: other\n",
        "- name: a\n  label: A\n  register: 1\n- name: a\n  label: B\n  register: 2\n",
        "- name: a\n  label: A\n  type: ram_timer_running\n  register: 1\n",
        "- name: a\n  label: A\n  register: 1\n  rt_field: 40\n",
        "[]",
    ],
)
def test_invalid_definitions(tmp_path: Path, body: str) -> None:
    with pytest.raises(SensorDefError):
        load_sensor_defs(write(tmp_path, body))


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SensorDefError):
        load_sensor_defs(tmp_path / "nope.yaml")
