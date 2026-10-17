"""Heating controller: degree-minute pushes (``docs/heating-control.md``).

All degree-minute values here are raw pump units (10 x displayed DM).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from nibe_service_usb.config import Config
from nibe_service_usb.core.errors import CommandError
from nibe_service_usb.core.safety import Guards
from nibe_service_usb.core.state import State
from nibe_service_usb.pump.interface import (
    Priority,
    PumpClient,
    PumpError,
    PumpOffline,
    WriteFailed,
)
from nibe_service_usb.pump.registers import decode

logger = logging.getLogger(__name__)

MODES = ("auto", "enable", "disable")

REG_DM = 40940
REG_START_COMP = 47206
REG_HEAT_DEMAND = 43110
REG_PRIORITY = 43086
REG_FSM = 43427
REG_INHIBIT = 43405

PRIORITY_HOT_WATER = 20
DAY_SECONDS = 86400.0

# Control sensors refreshed from the evaluation reads (name by register).
_SENSOR_BY_REGISTER = {
    REG_DM: "degree_minutes",
    REG_HEAT_DEMAND: "heat_demand",
    REG_PRIORITY: "op_priority",
    REG_FSM: "compressor_fsm",
    REG_INHIBIT: "compressor_inhibit",
    REG_START_COMP: "set_start_comp",
}


def _s32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >= 1 << 31 else value


def _s16(value: int) -> int:
    value &= 0xFFFF
    return value - (1 << 16) if value >= 1 << 15 else value


class HeatingController:
    def __init__(
        self,
        config: Config,
        pump: PumpClient,
        state: State,
        *,
        clock: Callable[[], float] = time.time,
        guards: Guards | None = None,
    ) -> None:
        self.config = config
        self.pump = pump
        self.state = state
        self.guards = guards or Guards(config.safety)
        self._clock = clock
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._address_checked = False
        self._address_invalid = False
        self._register_writes: collections.deque[float] = collections.deque()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Verify the RAM address if the pump is reachable (retried at each evaluation otherwise)."""
        if self.pump.online:
            async with self._lock:
                try:
                    await self._verify_address()
                except PumpError as exc:
                    logger.warning("Cannot verify the degree-minute RAM address yet: %s", exc)
        if self._address_invalid:
            self._publish_background()

    async def stop(self) -> None:
        async with self._lock:
            await self._cancel_timer()

    async def _cancel_timer(self) -> None:
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _publish_background(self) -> None:
        task = asyncio.get_running_loop().create_task(self.state.status_changed())
        task.add_done_callback(lambda _t: None)

    # ------------------------------------------------------------------ commands

    async def set_mode(self, mode: str) -> dict:
        if mode not in MODES:
            raise CommandError("invalid_params", f"mode must be one of {', '.join(MODES)}")
        async with self._lock:  # never cancel the timer in the middle of a pump write
            await self._cancel_timer()
            before = self._signature()
            self.state.heating.mode = mode
            written = await self._evaluate(before)
            if mode != "auto":
                self._task = asyncio.create_task(self._timer(), name="heating-timer")
        return {**asdict(self.state.heating), "written_dm": written}

    async def on_pump_online(self) -> None:
        """Pump came back (or restarted): evaluate right away and restart the push timer."""
        if self.state.heating.mode == "auto":
            return
        async with self._lock:
            await self._cancel_timer()
            await self._evaluate()
            self._task = asyncio.create_task(self._timer(), name="heating-timer")

    async def _timer(self) -> None:
        while True:
            await asyncio.sleep(self.config.heating.dm_push_interval)
            try:
                async with self._lock:
                    await self._evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Heating evaluation failed")

    # ------------------------------------------------------------------ evaluation

    def _targets(self, threshold: int | None) -> tuple[int, int | None]:
        """``(disable value, enable target)`` clamped to the configured bounds."""
        cfg = self.config.heating

        def clamp(value: int) -> int:
            return max(cfg.dm_min, min(cfg.dm_max, value))

        disable = clamp(cfg.dm_disable_value)
        enable = None if threshold is None else clamp(threshold + cfg.dm_enable_margin)
        return disable, enable

    async def _verify_address(self) -> None:
        """RM of the RAM address must decode to what RI 40940 returns. Sets the sticky invalid flag."""
        if self.config.heating.dm_write_method != "ram" or self._address_checked:
            return
        address = self.config.heating.dm_ram_address
        for _ in range(2):  # DM may be integrated between the two reads
            via_ri = _s32((await self.pump.read_many([REG_DM], priority=Priority.CONTROL))[0])
            via_rm = int.from_bytes(
                await self.pump.read_ram(address, 4, priority=Priority.CONTROL), "little", signed=True
            )
            if via_ri == via_rm:
                self._address_checked = True
                logger.info("Degree-minute RAM address 0x%x verified (DM raw %d)", address, via_ri)
                return
        self._address_checked = True
        self._address_invalid = True
        logger.error(
            "Degree-minute RAM address 0x%x decodes to %d but RI 40940 returns %d; refusing to write",
            address,
            via_rm,
            via_ri,
        )

    async def _refresh_guard_sensors(self) -> None:
        """Dedicated CONTROL read of the guard sensors so the data is seconds old."""
        sensors = []
        for name in self.guards.active_sensors():
            sensor = self.state.sensor_def(name)
            if sensor is not None and sensor.register is not None:
                sensors.append(sensor)
        if not sensors:
            return
        registers = [s.register for s in sensors]
        raws = await self.pump.read_many(registers, priority=Priority.CONTROL)
        ts = self._clock()
        changed: dict[str, Any] = {}
        for sensor, raw in zip(sensors, raws, strict=True):
            try:
                value = decode(sensor, raw)
            except (ValueError, TypeError):
                continue
            if self.state.set_sensor(sensor.name, value, ts) and sensor.enabled:
                changed[sensor.name] = self.state.get(sensor.name)
        await self.state.sensors_changed(changed)

    async def _update_sensors(self, raws: dict[int, int]) -> None:
        ts = self._clock()
        changed: dict[str, Any] = {}
        for register, raw in raws.items():
            sensor = self.state.sensor_def(_SENSOR_BY_REGISTER[register])
            if sensor is None:
                continue
            try:
                value = decode(sensor, raw)
            except (ValueError, TypeError):
                continue
            if self.state.set_sensor(sensor.name, value, ts) and sensor.enabled:
                changed[sensor.name] = self.state.get(sensor.name)
        await self.state.sensors_changed(changed)

    def _register_budget_left(self) -> bool:
        now = self._clock()
        while self._register_writes and now - self._register_writes[0] > DAY_SECONDS:
            self._register_writes.popleft()
        return len(self._register_writes) < self.config.heating.max_writes_per_day

    async def _write(self, value: int) -> None:
        cfg = self.config.heating
        if cfg.dm_write_method == "ram":
            await self.pump.write_ram(cfg.dm_ram_address, value.to_bytes(4, "little", signed=True))
        else:
            await self.pump.write(REG_DM, value)
            self._register_writes.append(self._clock())

    def _signature(self) -> tuple:
        heating = self.state.heating
        return (heating.mode, heating.active, heating.suspended_reason)

    async def _evaluate(self, before: tuple | None = None) -> int | None:
        """One evaluation (caller holds the lock). Returns the DM value written, if any."""
        heating = self.state.heating
        if before is None:
            before = self._signature()
        written: int | None = None
        try:
            written = await self._decide()
        except PumpOffline:
            self._set_status(active=False, reason="pump_offline", target=None)
        except PumpError as exc:
            logger.warning("Heating evaluation failed: %s", exc)
            self._set_status(active=False, reason="pump_offline", target=None)
        after = self._signature()
        if heating.mode == "auto":
            heating.next_push = None
        else:
            heating.next_push = self._clock() + self.config.heating.dm_push_interval
        if after != before:
            if heating.suspended_reason != before[2]:
                if heating.suspended_reason is not None:
                    logger.warning("Heating control suspended: %s", heating.suspended_reason)
                else:
                    logger.warning("Heating control resumed")
            await self.state.status_changed()
        return written

    def _set_status(self, *, active: bool, reason: str | None, target: int | None) -> None:
        heating = self.state.heating
        heating.active = active
        heating.suspended_reason = reason
        heating.target_dm = target

    async def _decide(self) -> int | None:
        heating = self.state.heating
        cfg = self.config.heating
        mode = heating.mode

        if mode == "auto":
            heating.target_dm = None
            self._set_status(
                active=False, reason="dm_address_invalid" if self._address_invalid else None, target=None
            )
            return None
        if not self.pump.online:
            self._set_status(active=False, reason="pump_offline", target=None)
            return None

        await self._verify_address()
        if self._address_invalid:
            self._set_status(active=False, reason="dm_address_invalid", target=None)
            return None

        if mode == "disable":
            await self._refresh_guard_sensors()

        registers = [REG_DM, REG_HEAT_DEMAND, REG_PRIORITY, REG_FSM, REG_INHIBIT, REG_START_COMP]
        values = await self.pump.read_many(registers, priority=Priority.CONTROL)
        raws = dict(zip(registers, values, strict=True))
        current = _s32(raws[REG_DM])
        heat_demand = raws[REG_HEAT_DEMAND]
        priority = raws[REG_PRIORITY]
        threshold = cfg.dm_start_threshold or _s16(raws[REG_START_COMP]) * 10
        disable_value, enable_target = self._targets(threshold)
        heating.current_dm = current
        await self._update_sensors(raws)

        if mode == "disable":
            target = disable_value
            guard = self.guards.check(self.state)
            if guard is not None:
                self._set_status(active=False, reason=guard, target=target)
                return None
        else:
            target = enable_target  # type: ignore[assignment]
        self._set_status(active=True, reason=None, target=target)

        if current == 0:
            # Safety rule: an exact 0 means the pump itself is holding the counter
            # (heating blocked by a rule such as stop heating, AUX block, op. mode).
            # Never write over it, whatever the other registers say.
            self._set_status(active=False, reason="dm_zero", target=target)
            return None

        if heat_demand == 0 or priority == PRIORITY_HOT_WATER:
            return None  # the pump holds DM at 0 / DM is frozen during a hot water charge

        if mode == "enable":
            # Same rule in both directions: write whenever it moves DM towards the
            # request, whatever the compressor is doing (a running compressor is kept
            # running by re-pushing the low target; a lower DM is never raised).
            if current <= target:
                return None
        else:
            if current > disable_value:
                return None  # already above the disable value: nothing to gain
            if cfg.dm_write_policy == "threshold" and current > threshold * cfg.dm_guard_fraction:
                return None  # economy mode: not yet close to the start threshold

        if cfg.dm_write_method == "register" and not self._register_budget_left():
            self._set_status(active=False, reason="write_budget", target=target)
            return None

        try:
            await self._write(target)
        except WriteFailed as exc:
            logger.warning("Degree-minute write of %d failed: %s (read-back %s)", target, exc, exc.readback)
            self._set_status(active=False, reason="write_failed", target=target)
            return None
        heating.last_push = self._clock()
        await self._read_back(target)
        return target

    async def _read_back(self, target: int) -> None:
        try:
            readback = _s32((await self.pump.read_many([REG_DM], priority=Priority.CONTROL))[0])
        except PumpOffline:
            raise
        except PumpError as exc:
            logger.warning("Read-back of degree minutes failed: %s", exc)
            return
        if readback != target:
            logger.warning(
                "Degree-minute write of %d read back as %d (pump may have clamped or reset it)",
                target,
                readback,
            )
        self.state.heating.current_dm = readback
        await self._update_sensors({REG_DM: readback})
