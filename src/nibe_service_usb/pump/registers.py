"""Sensor definitions (``sensors.yaml``), value decoding and wire metadata.

The YAML file has two top-level lists: ``sensors`` and ``alarms``. A bare list
is also accepted as the ``sensors`` list (custom files without alarms).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

TIERS = ("core", "settings", "identity", "diagnostic")
KINDS = ("temperature", "number", "enum", "binary", "duration", "alarm_list")

_INT_TYPES: dict[str, tuple[int, bool]] = {  # type -> (bits, signed)
    "s8": (8, True),
    "u8": (8, False),
    "s16": (16, True),
    "u16": (16, False),
    "s32": (32, True),
    "u32": (32, False),
}
# RAM based types: raw value is the bytes returned by ``RM``.
RAM_TYPES = ("ram_timer", "ram_s32", "ram_timer_running", "ram_timer_seconds")
TYPES = (*_INT_TYPES, *RAM_TYPES)

TEMPERATURE_SENTINELS = (-32768, -32767)  # open/short circuit, over range
RT_FIELD_MAX = 28

_SENSOR_KEYS = {
    "name",
    "label",
    "register",
    "rt_field",
    "ram",
    "type",
    "scale",
    "unit",
    "kind",
    "precision",
    "options",
    "tier",
    "enabled",
    "device_class",
    "state_class",
    "description",
}
_ALARM_KEYS = {"number", "register", "title"}


class SensorDefError(Exception):
    """``sensors.yaml`` is malformed."""


@dataclass(frozen=True)
class SensorDef:
    name: str
    label: str
    register: int | None
    rt_field: int | None
    ram: tuple[int, int] | None
    type: str  # s8 u8 s16 u16 s32 u32 | ram_timer ram_s32 ram_timer_running ram_timer_seconds
    scale: float
    unit: str
    kind: str  # temperature number enum binary duration alarm_list
    precision: int
    options: dict[int, str]
    tier: str  # core settings identity diagnostic
    enabled: bool
    device_class: str
    state_class: str
    description: str


@dataclass(frozen=True)
class AlarmDef:
    number: int
    register: int
    title: str


def default_sensors_path() -> Path:
    """Location of the packaged ``sensors.yaml``."""
    return Path(__file__).resolve().parent.parent / "sensors.yaml"


def _load_yaml(path: Path | None) -> dict[str, Any]:
    path = default_sensors_path() if path is None else Path(path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except OSError as exc:
        raise SensorDefError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SensorDefError(f"invalid YAML in {path}: {exc}") from exc
    if isinstance(data, list):
        return {"sensors": data}
    if not isinstance(data, dict):
        raise SensorDefError(f"{path}: top level must be a mapping with 'sensors' and 'alarms'")
    unknown = set(data) - {"sensors", "alarms"}
    if unknown:
        raise SensorDefError(f"{path}: unknown top-level key {sorted(unknown)[0]!r}")
    return data


def _int(where: str, key: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SensorDefError(f"{where}: {key} must be an integer, got {value!r}")
    return value


def _parse_sensor(index: int, row: Any) -> SensorDef:
    if not isinstance(row, dict):
        raise SensorDefError(f"sensors[{index}]: expected a mapping")
    name = row.get("name")
    if not isinstance(name, str) or not name:
        raise SensorDefError(f"sensors[{index}]: missing name")
    where = f"sensor {name!r}"
    unknown = set(row) - _SENSOR_KEYS
    if unknown:
        raise SensorDefError(f"{where}: unknown key {sorted(unknown)[0]!r}")
    label = row.get("label")
    if not isinstance(label, str) or not label:
        raise SensorDefError(f"{where}: missing label")

    register = row.get("register")
    if register is not None:
        register = _int(where, "register", register)
    rt_field = row.get("rt_field")
    if rt_field is not None:
        rt_field = _int(where, "rt_field", rt_field)
        if not 1 <= rt_field <= RT_FIELD_MAX:
            raise SensorDefError(f"{where}: rt_field must be 1..{RT_FIELD_MAX}")
    ram = row.get("ram")
    if ram is not None:
        if not (isinstance(ram, list | tuple) and len(ram) == 2):
            raise SensorDefError(f"{where}: ram must be [address, length]")
        ram = (_int(where, "ram address", ram[0]), _int(where, "ram length", ram[1]))
        if ram[1] < 1:
            raise SensorDefError(f"{where}: ram length must be positive")

    type_ = row.get("type", "s16")
    if type_ not in TYPES:
        raise SensorDefError(f"{where}: unknown type {type_!r}")
    kind = row.get("kind", "number")
    if kind not in KINDS:
        raise SensorDefError(f"{where}: unknown kind {kind!r}")
    tier = row.get("tier", "core")
    if tier not in TIERS:
        raise SensorDefError(f"{where}: unknown tier {tier!r}")

    scale = row.get("scale", 1.0)
    if isinstance(scale, bool) or not isinstance(scale, int | float) or scale == 0:
        raise SensorDefError(f"{where}: scale must be a non-zero number")
    precision = _int(where, "precision", row.get("precision", 0))
    if precision < 0:
        raise SensorDefError(f"{where}: precision must be >= 0")
    enabled = row.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SensorDefError(f"{where}: enabled must be true or false")

    raw_options = row.get("options") or {}
    if not isinstance(raw_options, dict):
        raise SensorDefError(f"{where}: options must be a mapping")
    options: dict[int, str] = {}
    for key, value in raw_options.items():
        if isinstance(key, bool) or not isinstance(key, int) or not isinstance(value, str):
            raise SensorDefError(f"{where}: options must map integers to strings, got {key!r}: {value!r}")
        options[key] = value
    if kind == "enum" and not options:
        raise SensorDefError(f"{where}: enum sensor needs options")

    strings: dict[str, str] = {}
    for key in ("unit", "device_class", "state_class", "description"):
        value = row.get(key, "")
        if not isinstance(value, str):
            raise SensorDefError(f"{where}: {key} must be a string")
        strings[key] = value

    if kind != "alarm_list":
        if type_ in RAM_TYPES:
            if ram is None or register is not None or rt_field is not None:
                raise SensorDefError(f"{where}: type {type_} needs 'ram' and no register/rt_field")
        else:
            if register is None:
                raise SensorDefError(f"{where}: missing register")
            if ram is not None:
                raise SensorDefError(f"{where}: 'ram' requires a ram_* type")

    return SensorDef(
        name=name,
        label=label,
        register=register,
        rt_field=rt_field,
        ram=ram,
        type=type_,
        scale=float(scale),
        unit=strings["unit"],
        kind=kind,
        precision=precision,
        options=options,
        tier=tier,
        enabled=enabled,
        device_class=strings["device_class"],
        state_class=strings["state_class"],
        description=strings["description"],
    )


def load_sensor_defs(path: Path | None) -> list[SensorDef]:
    """Load sensor definitions; ``None`` means the packaged ``sensors.yaml``."""
    rows = _load_yaml(path).get("sensors")
    if not isinstance(rows, list) or not rows:
        raise SensorDefError("'sensors' must be a non-empty list")
    sensors = [_parse_sensor(i, row) for i, row in enumerate(rows)]
    seen: set[str] = set()
    for sensor in sensors:
        if sensor.name in seen:
            raise SensorDefError(f"duplicate sensor name {sensor.name!r}")
        seen.add(sensor.name)
    return sensors


def load_alarm_defs(path: Path | None) -> list[AlarmDef]:
    """Load the alarm flag register list; a file without ``alarms`` gives an empty list."""
    rows = _load_yaml(path).get("alarms") or []
    if not isinstance(rows, list):
        raise SensorDefError("'alarms' must be a list")
    alarms: list[AlarmDef] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SensorDefError(f"alarms[{i}]: expected a mapping")
        unknown = set(row) - _ALARM_KEYS
        if unknown:
            raise SensorDefError(f"alarms[{i}]: unknown key {sorted(unknown)[0]!r}")
        if set(row) != _ALARM_KEYS or not isinstance(row["title"], str):
            raise SensorDefError(f"alarms[{i}]: needs number, register and title")
        alarms.append(
            AlarmDef(
                number=_int(f"alarms[{i}]", "number", row["number"]),
                register=_int(f"alarms[{i}]", "register", row["register"]),
                title=row["title"],
            )
        )
    numbers = [a.number for a in alarms]
    registers = [a.register for a in alarms]
    if len(set(numbers)) != len(numbers):
        raise SensorDefError("duplicate alarm number")
    if len(set(registers)) != len(registers):
        raise SensorDefError("duplicate alarm register")
    return alarms


def _normalise(type_: str, raw: int) -> int:
    """Wrap values outside the type's range (two's complement); in-range values are untouched."""
    bits, signed = _INT_TYPES[type_]
    low, high = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    if low <= raw <= high:
        return raw
    raw &= (1 << bits) - 1
    if signed and raw > high:
        raw -= 1 << bits
    return raw


def decode(sensor: SensorDef, raw: int | bytes) -> float | int | str | bool | None:
    """Convert a raw reading to its published value.

    * ``RI``/``RT`` register values are ints; RAM based types take the ``RM`` bytes.
    * Temperature sentinels (-32768, -32767) give ``None``.
    * ``binary`` gives a bool, ``enum`` the option string (``unknown_<n>`` for a
      value without an option), other kinds the scaled number rounded to
      ``precision`` (an int when the scale is 1).
    """
    if sensor.kind == "alarm_list":
        raise ValueError(f"{sensor.name} is derived from the alarm registers and has no raw value")

    if sensor.type in RAM_TYPES:
        if not isinstance(raw, bytes | bytearray):
            raise TypeError(f"{sensor.name}: type {sensor.type} needs bytes, got {type(raw).__name__}")
        if sensor.type == "ram_s32":
            if len(raw) < 4:
                raise ValueError(f"{sensor.name}: need 4 bytes, got {len(raw)}")
            value = int.from_bytes(raw[:4], "little", signed=True)
        else:
            if len(raw) < 3:
                raise ValueError(f"{sensor.name}: need 3 bytes (h, m, s), got {len(raw)}")
            hours, minutes, seconds = raw[0], raw[1], raw[2]
            if sensor.type == "ram_timer_running":
                return bool(hours or minutes or seconds)
            value = hours * 3600 + minutes * 60 + seconds
    else:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise TypeError(f"{sensor.name}: type {sensor.type} needs an int, got {type(raw).__name__}")
        value = _normalise(sensor.type, raw)

    if sensor.kind == "temperature" and value in TEMPERATURE_SENTINELS:
        return None
    if sensor.kind == "binary":
        return value != 0
    if sensor.kind == "enum":
        return sensor.options.get(value, f"unknown_{value}")
    if sensor.scale == 1.0:
        return value
    return round(value * sensor.scale, sensor.precision)


def metadata(sensor: SensorDef) -> dict:
    """Wire shape used by ``subscribe`` / ``list_sensors`` (without value and timestamp).

    ``name`` is the human readable label (the sensor key is the dict key in
    the response). ``options`` (list of strings) is present for enums only.
    Unset fields are ``null`` on the wire (never empty strings); ``precision``
    is only given for numeric kinds.
    """
    numeric = sensor.kind in ("temperature", "number", "duration")
    meta: dict[str, Any] = {
        "name": sensor.label,
        "kind": sensor.kind,
        "unit": sensor.unit or None,
        "precision": sensor.precision if numeric else None,
        "tier": sensor.tier,
        "device_class": sensor.device_class or None,
        "state_class": sensor.state_class or None,
        "description": sensor.description or None,
    }
    if sensor.kind == "enum":
        meta["options"] = list(sensor.options.values())
    return meta
