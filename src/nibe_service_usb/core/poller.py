"""Sensor poller and pump health check (``docs/sensors-and-polling.md``, ``docs/pump-interface.md``)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from nibe_service_usb.config import Config
from nibe_service_usb.core.state import State
from nibe_service_usb.pump.interface import (
    Priority,
    PumpClient,
    PumpError,
    PumpOffline,
)
from nibe_service_usb.pump.registers import AlarmDef, SensorDef, decode, load_alarm_defs

logger = logging.getLogger(__name__)

# Core sensors that change slowly: read only in the full cycle (``poll.interval``);
# everything else in the core tier (temperatures, pump speeds, states, alarms,
# degree minutes, periodic increase status, uptime) is read every ``poll.fast_interval``.
SLOW_CORE_SENSORS: frozenset[str] = frozenset({"comp_starts", "comp_hours", "comp_hours_hw"})
UPTIME_SENSOR = "uptime"
UPTIME_REGISTER = 43071
EEPROM_STORE_CALLS = 40969
EEPROM_WRITE_FAILURES = 40965
EEPROM_WRITE_FAILURES_BOOT = 40967
HEALTH_REGISTERS = (EEPROM_STORE_CALLS, EEPROM_WRITE_FAILURES, EEPROM_WRITE_FAILURES_BOOT, UPTIME_REGISTER)

AfterPoll = Callable[[], Awaitable[None] | None]


def seconds_until(now: datetime, hhmm: str) -> float:
    """Seconds from ``now`` (local, naive) until the next local ``HH:MM``."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


@dataclass
class _Plan:
    """What to read for one cycle."""

    rt: list[SensorDef] = field(default_factory=list)
    groups: list[tuple[str, list[SensorDef]]] = field(default_factory=list)  # register sensors per tier
    ram: list[tuple[tuple[int, int], list[SensorDef]]] = field(default_factory=list)
    alarms: bool = False


class SensorPoller:
    def __init__(
        self,
        config: Config,
        pump: PumpClient,
        state: State,
        sensors: list[SensorDef],
        *,
        alarms: list[AlarmDef] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.config = config
        self.pump = pump
        self.state = state
        self.sensors = [s for s in sensors if s.enabled]
        if alarms is None:
            path = Path(config.poll.sensors_file) if config.poll.sensors_file else None
            alarms = load_alarm_defs(path)
        self.alarms = alarms
        self._clock = clock
        self._monotonic = monotonic
        self._now = now
        # Called with no arguments when the display restarted (uptime decreased); wired by the app.
        self.on_restart: Callable[[], Awaitable[None] | None] | None = None
        # Called after every poll cycle (e.g. HotWaterController.refresh_periodic_status).
        self.on_polled: list[AfterPoll] = []
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._startup_pending = True
        self._cycle = 0
        self._last_poll: float | None = None
        self._next_full = 0.0
        self._next_fast = 0.0
        self._prev_uptime: int | None = None
        self._seen_uptime: int | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.pump.on_state_change(self._on_pump_state)
        await self._sync_pump()
        now = self._monotonic()
        self._next_full = now + self.config.poll.interval
        self._next_fast = now + self.config.poll.fast_interval
        self._tasks = [
            asyncio.create_task(self._loop(), name="poller"),
            asyncio.create_task(self._daily(), name="poller-daily"),
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _on_pump_state(self, online: bool) -> None:
        await self._sync_pump()
        await self.state.bus.publish("pump_state", {"online": online})
        if online:
            self._startup_pending = True
            self._wake.set()

    async def _sync_pump(self) -> None:
        """Copy the transport status into ``state.pump``; publish when online/port changed."""
        status = self.state.pump
        changed = status.online != self.pump.online or status.port != self.pump.port
        status.online = self.pump.online
        status.port = self.pump.port
        status.last_ok = self.pump.last_ok
        status.errors = self.pump.error_count
        if changed:
            await self.state.status_changed()

    async def _guarded(self, func: Callable[[], Awaitable[Any]]) -> None:
        try:
            await func()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Poller step %s failed", getattr(func, "__name__", func))

    async def _loop(self) -> None:
        while True:
            if self._startup_pending and self.pump.online:
                self._startup_pending = False
                await self._guarded(self.health_check)
                await self._guarded(self.poll_now)
                continue
            wake = min(self._next_full, self._next_fast)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), max(0.0, wake - self._monotonic()))
                continue  # woken by a pump state change: re-evaluate
            except TimeoutError:
                pass
            now = self._monotonic()
            if now >= self._next_full:
                await self._guarded(self.poll_now)
            elif now >= self._next_fast:
                await self._guarded(self._fast_cycle)

    async def _daily(self) -> None:
        while True:
            await asyncio.sleep(seconds_until(self._now(), self.config.pump.daily_check_time))
            await self._guarded(self.health_check)
            await asyncio.sleep(1.0)

    # ------------------------------------------------------------------ plans

    def _build_plan(self, select: Callable[[SensorDef], bool], *, alarms: bool = False) -> _Plan:
        plan = _Plan(alarms=alarms and bool(self.alarms))
        groups: dict[str, list[SensorDef]] = {}
        ram: dict[tuple[int, int], list[SensorDef]] = {}
        for sensor in self.sensors:
            if sensor.kind == "alarm_list" or not select(sensor):
                continue
            if sensor.ram is not None:
                ram.setdefault(sensor.ram, []).append(sensor)
            elif sensor.rt_field is not None:
                plan.rt.append(sensor)
            elif sensor.register is not None:
                tier = "core" if sensor.name == UPTIME_SENSOR else sensor.tier
                groups.setdefault(tier, []).append(sensor)
        plan.groups = list(groups.items())
        plan.ram = list(ram.items())
        return plan

    def _cycle_plan(self, tiers: set[str]) -> _Plan:
        def select(sensor: SensorDef) -> bool:
            return sensor.tier in tiers or (sensor.name == UPTIME_SENSOR and "core" in tiers)

        return self._build_plan(select, alarms="core" in tiers)

    # ------------------------------------------------------------------ execution

    def _apply(self, sensor: SensorDef, raw: int | bytes, ts: float, changed: dict) -> None:
        try:
            value = decode(sensor, raw)
        except (ValueError, TypeError) as exc:
            logger.warning("Cannot decode %s from %r: %s", sensor.name, raw, exc)
            self._fail([sensor], changed)
            return
        if sensor.name == UPTIME_SENSOR and isinstance(value, int):
            self._seen_uptime = value
        if self.state.set_sensor(sensor.name, value, ts):
            changed[sensor.name] = self.state.get(sensor.name)

    def _fail(self, sensors: list[SensorDef], changed: dict) -> None:
        for sensor in sensors:
            if self.state.sensor_failed(sensor.name):
                changed[sensor.name] = None

    async def _read_registers(
        self, sensors: list[SensorDef], ts: float, changed: dict, priority: Priority
    ) -> None:
        registers: list[int] = []
        for sensor in sensors:
            if sensor.register not in registers:
                registers.append(sensor.register)  # type: ignore[arg-type]
        raws = await self.pump.read_many(registers, priority=priority)
        by_register = dict(zip(registers, raws, strict=True))
        for sensor in sensors:
            self._apply(sensor, by_register[sensor.register], ts, changed)  # type: ignore[index]

    async def _execute(
        self, plan: _Plan, ts: float, *, priority: Priority = Priority.POLL
    ) -> tuple[dict, bool]:
        """Run the reads. Returns ``(changed values, pump went offline)``."""
        changed: dict[str, Any] = {}
        try:
            if plan.rt:
                try:
                    raws = await self.pump.read_temperatures(priority=priority)
                    for sensor in plan.rt:
                        self._apply(sensor, raws[sensor.rt_field - 1], ts, changed)  # type: ignore[operator]
                except PumpOffline:
                    raise
                except (PumpError, IndexError) as exc:
                    logger.warning("RT 1 failed (%s), falling back to RI", exc)
                    await self._guard_group(plan.rt, ts, changed, priority)
            for tier, sensors in plan.groups:
                await self._guard_group(sensors, ts, changed, priority, tier)
            for (address, length), sensors in plan.ram:
                try:
                    raw = await self.pump.read_ram(address, length, priority=priority)
                except PumpOffline:
                    raise
                except PumpError as exc:
                    logger.warning("RM 0x%x failed: %s", address, exc)
                    self._fail(sensors, changed)
                    continue
                for sensor in sensors:
                    self._apply(sensor, raw, ts, changed)
            if plan.alarms:
                await self._read_alarms(ts, changed, priority)
        except PumpOffline:
            return changed, True
        return changed, False

    async def _guard_group(
        self, sensors: list[SensorDef], ts: float, changed: dict, priority: Priority, tier: str = "rt"
    ) -> None:
        sensors = [s for s in sensors if s.register is not None]
        try:
            await self._read_registers(sensors, ts, changed, priority)
        except PumpOffline:
            raise
        except PumpError as exc:
            logger.warning("Reading %s sensors failed: %s", tier, exc)
            self._fail(sensors, changed)

    async def _read_alarms(self, ts: float, changed: dict, priority: Priority) -> None:
        name = "active_alarms"
        try:
            raws = await self.pump.read_many([a.register for a in self.alarms], priority=priority)
        except PumpOffline:
            raise
        except PumpError as exc:
            logger.warning("Reading alarm flags failed: %s", exc)
            if self.state.sensor_def(name) is not None and self.state.sensor_failed(name):
                changed[name] = None
            return
        if self.state.sensor_def(name) is None:
            return
        active = [
            {"number": alarm.number, "title": alarm.title}
            for alarm, raw in zip(self.alarms, raws, strict=True)
            if raw != 0
        ]
        if self.state.set_sensor(name, active, ts):
            changed[name] = active

    # ------------------------------------------------------------------ cycles

    async def poll_now(self, *, tiers: set[str] | None = None) -> None:
        """One poll cycle: core + control (+ settings when due or requested)."""
        async with self._lock:
            await self._poll_cycle(tiers)

    async def ensure_fresh(self) -> None:
        """Poll unless the last cycle is younger than ``poll.interval``."""
        async with self._lock:
            if (
                self._last_poll is not None
                and self._monotonic() - self._last_poll < self.config.poll.interval
            ):
                return
            await self._poll_cycle(None)

    def _reschedule(self) -> None:
        now = self._monotonic()
        self._next_full = now + self.config.poll.interval
        self._next_fast = now + self.config.poll.fast_interval
        self._wake.set()

    async def _poll_cycle(self, tiers: set[str] | None) -> None:
        self._reschedule()
        if not self.pump.online:
            await self._sync_pump()
            return
        default_tiers = tiers is None
        if tiers is None:
            tiers = {"core"}
            if self.config.poll.diagnostic:
                tiers.add("diagnostic")
            if self._cycle % max(1, self.config.poll.settings_every) == 0:
                tiers.add("settings")
        self._seen_uptime = None
        changed, offline = await self._execute(self._cycle_plan(tiers), self._clock())
        await self.state.sensors_changed(changed)
        if not offline:
            self._last_poll = self._monotonic()
            if default_tiers:
                self._cycle += 1
        await self._sync_pump()
        if self._seen_uptime is not None and self._note_uptime(self._seen_uptime):
            await self._handle_restart(run_health_check=True)
        await self._run_hooks()

    def _is_fast(self, sensor: SensorDef) -> bool:
        if sensor.name == UPTIME_SENSOR:
            return True
        return sensor.tier == "core" and sensor.name not in SLOW_CORE_SENSORS

    async def _fast_cycle(self) -> None:
        """Every ``poll.fast_interval``: temperatures, pump speeds, states, alarms, degree minutes."""
        async with self._lock:
            self._next_fast = self._monotonic() + self.config.poll.fast_interval
            if not self.pump.online:
                return
            self._seen_uptime = None
            plan = self._build_plan(self._is_fast, alarms=True)
            changed, offline = await self._execute(plan, self._clock())
            await self.state.sensors_changed(changed)
            await self._sync_pump()
            if self._seen_uptime is not None and self._note_uptime(self._seen_uptime):
                await self._handle_restart(run_health_check=True)
            if not offline:
                await self._run_hooks()

    async def _run_hooks(self) -> None:
        for hook in list(self.on_polled):
            try:
                result = hook()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Post-poll hook failed")

    # ------------------------------------------------------------------ restart detection

    def _note_uptime(self, value: int) -> bool:
        """Record the uptime; True when it decreased (the display restarted)."""
        previous, self._prev_uptime = self._prev_uptime, value
        self.state.identity.uptime = value
        if previous is not None and value < previous:
            self.state.identity.last_restart = self._clock() - value
            return True
        return False

    async def _handle_restart(self, *, run_health_check: bool) -> None:
        logger.warning("Pump display restarted (uptime decreased)")
        if run_health_check:
            await self.health_check()
        await self.state.status_changed()
        if self.on_restart is not None:
            try:
                result = self.on_restart()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Restart callback failed")

    # ------------------------------------------------------------------ health check

    async def health_check(self) -> None:
        """Identity, clock, EEPROM health and uptime -> ``state.identity``; also reads settings."""
        if not self.pump.online:
            return
        identity = self.state.identity
        try:
            info = await self.pump.identity()
        except PumpOffline:
            return
        except PumpError as exc:
            logger.warning("Health check: identity failed: %s", exc)
        else:
            self._check_identity(info)
        try:
            clk = await self.pump.clock()
        except PumpOffline:
            return
        except PumpError as exc:
            logger.warning("Health check: clock failed: %s", exc)
        else:
            identity.clock_state = clk.state(self.config.pump.max_clock_drift)
            # The drift is meaningless while the display clock is unset (it counts from 2010-01-01).
            identity.clock_drift = None if identity.clock_state == "unset" else round(clk.drift, 1)
            if identity.clock_state == "drifting":
                logger.warning("Pump clock drifts by %.0f s", clk.drift)

        ts = self._clock()
        changed: dict[str, Any] = {}
        restarted = False
        try:
            raws = await self.pump.read_many(list(HEALTH_REGISTERS))
        except PumpOffline:
            return
        except PumpError as exc:
            logger.warning("Health check: EEPROM registers failed: %s", exc)
        else:
            calls, failures, boot_failures, uptime = raws
            increased = (
                identity.eeprom_write_failures is not None and failures > identity.eeprom_write_failures
            )
            identity.eeprom_store_calls = calls
            identity.eeprom_write_failures = failures
            identity.eeprom_write_failed = boot_failures != 0 or increased
            if identity.eeprom_write_failed:
                logger.warning("EEPROM write failures: persisted %d, since boot %d", failures, boot_failures)
            restarted = self._note_uptime(uptime)
            by_register = dict(zip(HEALTH_REGISTERS, raws, strict=True))
            for sensor in self.sensors:
                if sensor.register in by_register and sensor.ram is None:
                    self._apply(sensor, by_register[sensor.register], ts, changed)

        plan = self._build_plan(
            lambda s: s.tier in ("settings", "identity") and s.register not in HEALTH_REGISTERS
        )
        more, offline = await self._execute(plan, ts)
        changed.update(more)
        await self.state.sensors_changed(changed)
        if offline:
            return
        base = self.state.get("base_version")
        if identity.base_version is not None and base is not None and base != identity.base_version:
            logger.warning(
                "PCA base version mismatch: RV says %s, R32041 says %s", identity.base_version, base
            )
        identity.last_health_check = self._clock()
        await self._sync_pump()
        await self.state.status_changed()
        if restarted:
            await self._handle_restart(run_health_check=False)

    def _check_identity(self, info: Any) -> None:
        identity = self.state.identity
        for label, old, new in (
            ("product", identity.product, info.product),
            ("software version", identity.software_version, info.software_version),
            ("product code", identity.product_code, info.product_code),
        ):
            if old is not None and old != new:
                logger.warning(
                    "Pump %s changed from %r to %r (firmware update or different unit)", label, old, new
                )
        identity.product = info.product
        identity.software_version = info.software_version
        identity.base_version = info.base_version
        identity.product_code = info.product_code
