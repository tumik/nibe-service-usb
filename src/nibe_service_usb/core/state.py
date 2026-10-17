"""Shared server state: sensor values and the status sections of ``get_status``."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from nibe_service_usb.core.events import EventBus
from nibe_service_usb.pump.registers import SensorDef, metadata

logger = logging.getLogger(__name__)

FAILURES_BEFORE_UNAVAILABLE = 3


@dataclass
class SensorValue:
    value: Any
    ts: float
    failures: int = 0


@dataclass
class PumpStatus:
    online: bool = False
    port: str = ""
    last_ok: float | None = None
    errors: int = 0


@dataclass
class HeatingStatus:
    mode: str = "auto"  # auto | enable | disable
    active: bool = False
    suspended_reason: str | None = None
    last_push: float | None = None
    next_push: float | None = None
    target_dm: int | None = None
    current_dm: int | None = None


@dataclass
class PeriodicIncreaseStatus:
    running: bool = False
    remaining_seconds: int = 0
    hold: bool = False
    enabled: bool | None = None


@dataclass
class HotWaterStatus:
    mode: str | None = None  # eco | normal | lux (comfort setting)
    effective_mode: str | None = None
    reason: str | None = None
    periodic_increase: PeriodicIncreaseStatus = field(default_factory=PeriodicIncreaseStatus)


@dataclass
class WatchdogStatus:
    ha_alive: bool = False
    last_heartbeat: float | None = None
    timeout: float = 300.0
    reverted_at: float | None = None


@dataclass
class IdentityStatus:
    product: str | None = None
    software_version: str | None = None
    base_version: int | None = None
    product_code: str | None = None
    clock_state: str | None = None
    clock_drift: float | None = None
    uptime: int | None = None
    last_restart: float | None = None
    eeprom_store_calls: int | None = None
    eeprom_write_failures: int | None = None
    eeprom_write_failed: bool = False
    last_health_check: float | None = None


class State:
    """Holds sensor values and status sections; publishes changes on the bus."""

    def __init__(
        self,
        sensors: list[SensorDef],
        bus: EventBus,
        version: str,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus
        self.version = version
        self._clock = clock
        self._monotonic = monotonic
        self._started = monotonic()
        self._defs: dict[str, SensorDef] = {s.name: s for s in sensors}
        self._values: dict[str, SensorValue] = {}
        self.pump = PumpStatus()
        self.heating = HeatingStatus()
        self.hot_water = HotWaterStatus()
        self.watchdog = WatchdogStatus()
        self.identity = IdentityStatus()
        self.clients = 0

    # ------------------------------------------------------------------ sensors

    def sensor_def(self, name: str) -> SensorDef | None:
        return self._defs.get(name)

    def set_sensor(self, name: str, value: Any, ts: float) -> bool:
        """Store a reading; True if the (precision rounded) value differs from the stored one.

        The timestamp and the failure counter are updated on every successful read.
        """
        sensor = self._defs[name]
        value = self._round(sensor, value)
        current = self._values.get(name)
        if current is None:
            self._values[name] = SensorValue(value, ts)
            return value is not None
        changed = current.value != value or type(current.value) is not type(value)
        current.value = value
        current.ts = ts
        current.failures = 0
        return changed

    @staticmethod
    def _round(sensor: SensorDef, value: Any) -> Any:
        if isinstance(value, float):
            return round(value, sensor.precision)
        return value

    def sensor_failed(self, name: str) -> bool:
        """Count a failed read; after three in a row the value becomes None. True if it changed."""
        if name not in self._defs:
            raise KeyError(name)
        current = self._values.get(name)
        if current is None:
            return False
        current.failures += 1
        if current.failures >= FAILURES_BEFORE_UNAVAILABLE and current.value is not None:
            current.value = None
            return True
        return False

    def get(self, name: str) -> Any:
        current = self._values.get(name)
        return None if current is None else current.value

    def sensor_age(self, name: str) -> float | None:
        current = self._values.get(name)
        if current is None:
            return None
        return self._clock() - current.ts

    def sensor_snapshot(self, names: Iterable[str] | None = None) -> dict:
        """``{name: {value, ts, ...metadata}}`` for enabled sensors (all, or just ``names``)."""
        wanted = list(self._defs) if names is None else list(names)
        out: dict[str, dict] = {}
        for name in wanted:
            sensor = self._defs.get(name)
            if sensor is None or not sensor.enabled:
                continue
            current = self._values.get(name)
            entry = {
                "value": None if current is None else current.value,
                "ts": None if current is None else current.ts,
            }
            entry.update(metadata(sensor))
            out[name] = entry
        return out

    # ------------------------------------------------------------------ status

    async def status_changed(self) -> None:
        await self.bus.publish("status_changed", self.snapshot()["status"])

    async def sensors_changed(self, values: dict) -> None:
        if values:
            await self.bus.publish("sensor_changed", {"values": values})

    def status_dict(self) -> dict:
        return {
            "version": self.version,
            "uptime": self._monotonic() - self._started,
            "pump": asdict(self.pump),
            "heating": asdict(self.heating),
            "hot_water": asdict(self.hot_water),
            "watchdog": asdict(self.watchdog),
            "identity": asdict(self.identity),
            "clients": self.clients,
        }

    def snapshot(self) -> dict:
        return {"status": self.status_dict(), "sensors": self.sensor_snapshot()}
