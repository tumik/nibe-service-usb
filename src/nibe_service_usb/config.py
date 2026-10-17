"""Configuration: dataclasses, TOML loading, validation, data directory.

See ``docs/configuration.md``. Every key has a default; unknown keys are an
error naming the dotted key; values are type- and range-checked.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

ENV_DATA_DIR = "NIBE_SERVICE_USB_DATA_DIR"
ENV_CONFIG = "NIBE_SERVICE_USB_CONFIG"
DEFAULT_DATA_DIR = "~/.local/share/nibe-service-usb"

LOG_LEVELS = ("debug", "info", "warning", "error", "critical")
HOT_WATER_MODES = ("eco", "normal", "lux")


class ConfigError(Exception):
    """Invalid configuration (unknown key, wrong type, value out of range)."""


@dataclass(frozen=True)
class PumpConfig:
    port: str = "/dev/ttyACM0"
    baudrate: int = 115200
    rts: bool = True
    dtr: bool = True
    ri_batch_size: int = 9
    max_line_length: int = 61
    request_timeout: float = 3.0
    retries: int = 2
    offline_after: float = 60.0
    daily_check_time: str = "03:30"
    max_clock_drift: float = 120.0


@dataclass(frozen=True)
class ServerConfig:
    listen_host: str = "0.0.0.0"
    listen_port: int = 9477
    max_clients: int = 4
    idle_timeout: float = 600.0


@dataclass(frozen=True)
class DiscoveryConfig:
    enabled: bool = True
    name: str = "Nibe heat pump"
    model: str = ""


@dataclass(frozen=True)
class WatchdogConfig:
    timeout: float = 300.0


@dataclass(frozen=True)
class DefaultsConfig:
    hot_water_mode: str | None = "normal"


@dataclass(frozen=True)
class PollConfig:
    interval: float = 600.0
    settings_every: int = 6
    fast_interval: float = 60.0
    sensors_file: str = ""
    diagnostic: bool = False


@dataclass(frozen=True)
class HeatingConfig:
    dm_push_interval: float = 600.0
    dm_disable_value: int = -1
    dm_start_threshold: int = 0
    dm_enable_margin: int = 1
    dm_write_method: str = "ram"
    dm_ram_address: int = 0x2038AF18
    dm_write_policy: str = "always"
    dm_guard_fraction: float = 0.5
    max_writes_per_day: int = 100
    dm_min: int = -15000
    dm_max: int = 0


@dataclass(frozen=True)
class SafetyConfig:
    min_room_temp: float | None = None
    min_supply_temp: float | None = 20.0
    min_outdoor_temp: float | None = -15.0
    max_sensor_age: float = 900.0
    hysteresis: float = 1.0


@dataclass(frozen=True)
class DebugWebsiteConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 0
    feeds_watchdog: bool = False


@dataclass(frozen=True)
class LogConfig:
    level: str = "info"


@dataclass(frozen=True)
class Config:
    pump: PumpConfig
    server: ServerConfig
    discovery: DiscoveryConfig
    watchdog: WatchdogConfig
    defaults: DefaultsConfig
    poll: PollConfig
    heating: HeatingConfig
    safety: SafetyConfig
    debug_website: DebugWebsiteConfig
    log: LogConfig
    data_dir: Path


# Section name -> dataclass, in file order.
SECTIONS: dict[str, type] = {
    "pump": PumpConfig,
    "server": ServerConfig,
    "discovery": DiscoveryConfig,
    "watchdog": WatchdogConfig,
    "defaults": DefaultsConfig,
    "poll": PollConfig,
    "heating": HeatingConfig,
    "safety": SafetyConfig,
    "debug_website": DebugWebsiteConfig,
    "log": LogConfig,
}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _convert(key: str, type_name: str, value: Any) -> Any:
    """Check ``value`` against the field type name and normalise it."""
    if key.endswith("heating.dm_ram_address"):
        if isinstance(value, bool):
            raise ConfigError(f"{key}: expected integer or hex string")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip(), 0)
            except ValueError:
                raise ConfigError(f"{key}: not an integer or hex string: {value!r}") from None
        raise ConfigError(f"{key}: expected integer or hex string")
    if type_name == "str":
        if not isinstance(value, str):
            raise ConfigError(f"{key}: expected string, got {value!r}")
        return value
    if type_name == "str | None":
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ConfigError(f"{key}: expected string, got {value!r}")
        return value
    if type_name == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{key}: expected true or false, got {value!r}")
        return value
    if type_name == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{key}: expected integer, got {value!r}")
        return value
    if type_name == "float":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{key}: expected number, got {value!r}")
        return float(value)
    if type_name == "float | None":
        if value is None or value is False:  # TOML cannot express None; `false` disables the guard
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{key}: expected number or false, got {value!r}")
        return float(value)
    raise AssertionError(f"unsupported config type {type_name!r} for {key}")  # pragma: no cover


def _build_section(name: str, cls: type, raw: Any) -> Any:
    if not isinstance(raw, dict):
        raise ConfigError(f"{name}: expected a table, got {raw!r}")
    known = {f.name: f for f in fields(cls)}
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in known:
            raise ConfigError(f"unknown config key: {name}.{key}")
        values[key] = _convert(f"{name}.{key}", str(known[key].type), value)
    return cls(**values)


def _check(ok: bool, key: str, message: str) -> None:
    if not ok:
        raise ConfigError(f"{key}: {message}")


def _validate(cfg: Config) -> Config:
    p, s, w, d, po, h, sa, db, lg = (
        cfg.pump,
        cfg.server,
        cfg.watchdog,
        cfg.defaults,
        cfg.poll,
        cfg.heating,
        cfg.safety,
        cfg.debug_website,
        cfg.log,
    )
    _check(1 <= p.ri_batch_size <= 10, "pump.ri_batch_size", f"must be 1..10, got {p.ri_batch_size}")
    _check(16 <= p.max_line_length <= 61, "pump.max_line_length", f"must be 16..61, got {p.max_line_length}")
    _check(p.baudrate > 0, "pump.baudrate", "must be positive")
    _check(p.request_timeout > 0, "pump.request_timeout", "must be positive")
    _check(p.retries >= 0, "pump.retries", "must be >= 0")
    _check(p.offline_after > 0, "pump.offline_after", "must be positive")
    _check(
        _TIME_RE.match(p.daily_check_time) is not None,
        "pump.daily_check_time",
        f'must be "HH:MM", got {p.daily_check_time!r}',
    )
    _check(p.max_clock_drift >= 0, "pump.max_clock_drift", "must be >= 0")

    _check(0 <= s.listen_port <= 65535, "server.listen_port", f"must be 0..65535, got {s.listen_port}")
    _check(s.max_clients >= 1, "server.max_clients", "must be >= 1")
    _check(s.idle_timeout > 0, "server.idle_timeout", "must be positive")

    _check(w.timeout >= 120, "watchdog.timeout", f"must be >= 120 seconds, got {w.timeout:g}")

    _check(
        d.hot_water_mode is None or d.hot_water_mode in HOT_WATER_MODES,
        "defaults.hot_water_mode",
        f"must be one of {', '.join(HOT_WATER_MODES)} (or empty), got {d.hot_water_mode!r}",
    )

    _check(po.interval >= 60, "poll.interval", f"must be >= 60 seconds, got {po.interval:g}")
    _check(po.settings_every >= 1, "poll.settings_every", "must be >= 1")
    _check(po.fast_interval >= 10, "poll.fast_interval", f"must be >= 10 seconds, got {po.fast_interval:g}")
    _check(po.fast_interval <= po.interval, "poll.fast_interval", "must not exceed poll.interval")

    _check(h.dm_push_interval > 0, "heating.dm_push_interval", "must be positive")
    _check(
        h.dm_write_method in ("ram", "register"),
        "heating.dm_write_method",
        f"must be 'ram' or 'register', got {h.dm_write_method!r}",
    )
    _check(
        h.dm_write_policy in ("always", "threshold"),
        "heating.dm_write_policy",
        f"must be 'always' or 'threshold', got {h.dm_write_policy!r}",
    )
    _check(h.dm_min >= -15000, "heating.dm_min", f"must be >= -15000, got {h.dm_min}")
    _check(h.dm_max <= 0, "heating.dm_max", f"must be <= 0, got {h.dm_max}")
    _check(h.dm_min <= h.dm_max, "heating.dm_min", "must not exceed heating.dm_max")
    _check(
        h.dm_min <= h.dm_disable_value <= h.dm_max,
        "heating.dm_disable_value",
        f"must be within [dm_min, dm_max] = [{h.dm_min}, {h.dm_max}], got {h.dm_disable_value}",
    )
    _check(h.dm_enable_margin >= 0, "heating.dm_enable_margin", "must be >= 0")
    _check(0 <= h.dm_guard_fraction <= 1, "heating.dm_guard_fraction", "must be within 0..1")
    _check(h.max_writes_per_day >= 1, "heating.max_writes_per_day", "must be >= 1")
    _check(0 <= h.dm_ram_address <= 0xFFFFFFFF, "heating.dm_ram_address", "must fit in 32 bits")

    _check(sa.max_sensor_age > 0, "safety.max_sensor_age", "must be positive")
    _check(sa.hysteresis >= 0, "safety.hysteresis", "must be >= 0")

    _check(0 <= db.port <= 65535, "debug_website.port", f"must be 0..65535, got {db.port}")

    _check(lg.level in LOG_LEVELS, "log.level", f"must be one of {', '.join(LOG_LEVELS)}, got {lg.level!r}")
    return cfg


def resolve_data_dir(explicit: Path | None = None) -> Path:
    """Explicit argument, else ``$NIBE_SERVICE_USB_DATA_DIR``, else ``~/.local/share/...``."""
    if explicit is not None:
        return Path(explicit).expanduser()
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        return Path(env).expanduser()
    return Path(DEFAULT_DATA_DIR).expanduser()


def load_config(
    path: Path | None,
    *,
    data_dir: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    """Load, override and validate the configuration.

    ``path=None`` gives all defaults. ``overrides`` use dotted keys such as
    ``"server.listen_port"``; they are applied before validation.
    """
    raw: dict[str, Any] = {}
    if path is not None:
        try:
            with open(path, "rb") as fh:
                raw = tomllib.load(fh)
        except OSError as exc:
            raise ConfigError(f"cannot read config file {path}: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    for key in raw:
        if key not in SECTIONS:
            raise ConfigError(f"unknown config key: {key}")

    for dotted, value in (overrides or {}).items():
        section, _, key = dotted.partition(".")
        if section not in SECTIONS or not key:
            raise ConfigError(f"unknown config key: {dotted}")
        table = raw.setdefault(section, {})
        if not isinstance(table, dict):
            raise ConfigError(f"{section}: expected a table")
        table[key] = value

    built = {name: _build_section(name, cls, raw.get(name, {})) for name, cls in SECTIONS.items()}
    built["log"] = replace(built["log"], level=built["log"].level.lower())  # accept "INFO"
    cfg = Config(**built, data_dir=resolve_data_dir(data_dir))
    return _validate(cfg)


def _toml_value(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return repr(value)


def format_config(cfg: Config) -> str:
    """Render the effective configuration as TOML-ish text (for ``check-config``)."""
    lines = [f'# data_dir = "{cfg.data_dir}"']
    for name in SECTIONS:
        section = getattr(cfg, name)
        lines.append("")
        lines.append(f"[{name}]")
        for f in fields(section):
            value = getattr(section, f.name)
            if name == "heating" and f.name == "dm_ram_address":
                lines.append(f'{f.name} = "0x{value:08x}"')
            elif value is None:
                lines.append(f"# {f.name} = (unset)")
            else:
                lines.append(f"{f.name} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"
