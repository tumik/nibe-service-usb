"""Hot water controller: comfort mode and periodic increase trigger (``docs/pump-interface.md``)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from nibe_service_usb.config import Config
from nibe_service_usb.core.errors import CommandError
from nibe_service_usb.core.poller import SensorPoller
from nibe_service_usb.core.state import State
from nibe_service_usb.pump.interface import (
    Priority,
    PumpClient,
    PumpError,
    PumpOffline,
    PumpTimeout,
    WriteFailed,
)
from nibe_service_usb.pump.registers import decode

logger = logging.getLogger(__name__)

REG_COMFORT = 47041
REG_EFFECTIVE = 43109
REG_REASON = 40313
REG_PI_ENABLE = 47050
REG_PI_HOLD = 43014
PI_TIMER = (0x2066718C, 3)
HW_MODE_PERIODIC = 3

MODE_VALUES = {"eco": 0, "normal": 1, "lux": 2}
VALUE_MODES = {v: k for k, v in MODE_VALUES.items()}

RATE_LIMIT_SECONDS = 600.0
INITIAL_WAIT = 5.0
POLL_STEP = 3.0
POLL_TOTAL = 15.0


def _timer_seconds(raw: bytes) -> int:
    return raw[0] * 3600 + raw[1] * 60 + raw[2]


class HotWaterController:
    def __init__(
        self,
        config: Config,
        pump: PumpClient,
        state: State,
        poller: SensorPoller,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.pump = pump
        self.state = state
        self.poller = poller
        self._sleep = sleep
        self._monotonic = monotonic
        self._clock = clock
        self._last_trigger: float | None = None
        self._trigger_lock = asyncio.Lock()
        poller.on_polled.append(self.refresh_periodic_status)

    # ------------------------------------------------------------------ errors

    @staticmethod
    def _command_error(exc: PumpError) -> CommandError:
        if isinstance(exc, PumpOffline):
            return CommandError("pump_offline", str(exc))
        if isinstance(exc, PumpTimeout):
            return CommandError("pump_timeout", str(exc))
        if isinstance(exc, WriteFailed):
            return CommandError("write_failed", f"{exc} (read-back {exc.readback})")
        return CommandError("internal", str(exc))

    # ------------------------------------------------------------------ status

    async def refresh_periodic_status(self) -> None:
        """Update ``state.hot_water`` from the sensor values (called after each poll)."""
        hw = self.state.hot_water
        pi = hw.periodic_increase
        before = (
            hw.mode,
            hw.effective_mode,
            hw.reason,
            pi.running,
            pi.remaining_seconds,
            pi.hold,
            pi.enabled,
        )
        comfort = self.state.get("hw_comfort")
        if comfort is not None:
            hw.mode = comfort
        effective = self.state.get("hw_mode")
        if effective is not None:
            hw.effective_mode = effective
        reason = self.state.get("hw_reason")
        if reason is not None:
            hw.reason = reason
        running = self.state.get("pi_running")
        if running is not None:
            pi.running = bool(running)
        remaining = self.state.get("pi_remaining")
        if remaining is not None:
            pi.remaining_seconds = int(remaining) if pi.running else 0
        hold = self.state.get("pi_hold")
        if hold is not None:
            pi.hold = bool(hold) and pi.running  # only meaningful while running
        enabled = self.state.get("pi_enabled")
        if enabled is not None:
            pi.enabled = bool(enabled)
        if (
            hw.mode,
            hw.effective_mode,
            hw.reason,
            pi.running,
            pi.remaining_seconds,
            pi.hold,
            pi.enabled,
        ) != before:
            await self.state.status_changed()

    async def _publish_values(self, readings: dict[str, Any]) -> None:
        """Store freshly read sensor values and publish the changed ones."""
        ts = self._clock()
        changed: dict[str, Any] = {}
        for name, value in readings.items():
            if self.state.sensor_def(name) is not None and self.state.set_sensor(name, value, ts):
                changed[name] = self.state.get(name)
        await self.state.sensors_changed(changed)

    def _decode(self, name: str, raw: int | bytes) -> Any:
        sensor = self.state.sensor_def(name)
        return None if sensor is None else decode(sensor, raw)

    # ------------------------------------------------------------------ comfort mode

    async def set_mode(self, mode: str) -> dict:
        if mode not in MODE_VALUES:
            raise CommandError("invalid_params", f"mode must be one of {', '.join(MODE_VALUES)}")
        if not self.pump.online:
            raise CommandError("pump_offline", "pump is not reachable")
        try:
            await self.pump.write(REG_COMFORT, MODE_VALUES[mode], priority=Priority.COMMAND)
            comfort, effective, reason = await self.pump.read_many(
                [REG_COMFORT, REG_EFFECTIVE, REG_REASON], priority=Priority.COMMAND
            )
        except PumpError as exc:
            raise self._command_error(exc) from exc
        actual = VALUE_MODES.get(comfort, f"unknown_{comfort}")
        await self._publish_values(
            {
                "hw_comfort": actual,
                "hw_mode": self._decode("hw_mode", effective),
                "hw_reason": self._decode("hw_reason", reason),
            }
        )
        hw = self.state.hot_water
        hw.mode = actual
        hw.effective_mode = self._decode("hw_mode", effective) or hw.effective_mode
        reason_value = self._decode("hw_reason", reason)
        if reason_value is not None:
            hw.reason = reason_value
        await self.state.status_changed()
        return {"mode": actual}

    # ------------------------------------------------------------------ periodic increase

    async def _read_timer(self) -> int:
        raw = await self.pump.read_ram(*PI_TIMER, priority=Priority.COMMAND)
        return _timer_seconds(raw)

    async def trigger_periodic_increase(self) -> dict:
        if self._trigger_lock.locked():
            raise CommandError("already_running", "a trigger is in progress")
        async with self._trigger_lock:
            if not self.pump.online:
                raise CommandError("pump_offline", "pump is not reachable")
            try:
                return await self._trigger()
            except PumpError as exc:
                raise self._command_error(exc) from exc

    async def _trigger(self) -> dict:
        previous = await self.pump.read(REG_PI_ENABLE, priority=Priority.COMMAND)
        if await self._read_timer() != 0:
            raise CommandError("already_running", "periodic increase is already running")
        now = self._monotonic()
        if self._last_trigger is not None and now - self._last_trigger < RATE_LIMIT_SECONDS:
            raise CommandError("rate_limited", "one trigger per 10 minutes")
        self._last_trigger = now

        await self.pump.write(REG_PI_ENABLE, 0, priority=Priority.COMMAND)
        try:
            await self._sleep(INITIAL_WAIT)
            await self.pump.write(REG_PI_ENABLE, 1, priority=Priority.COMMAND)
        except BaseException:
            # Do not leave the enable bit cleared if the second write did not happen.
            if previous != 0:
                try:
                    await self.pump.write(REG_PI_ENABLE, previous, priority=Priority.COMMAND)
                except Exception:
                    logger.exception("Could not restore the periodic increase enable bit")
            raise

        elapsed = 0.0
        while elapsed < POLL_TOTAL:
            await self._sleep(POLL_STEP)
            elapsed += POLL_STEP
            remaining = await self._read_timer()
            effective = await self.pump.read(REG_EFFECTIVE, priority=Priority.COMMAND)
            if remaining != 0 or effective == HW_MODE_PERIODIC:
                await self._after_start()
                return {
                    "started": True,
                    "remaining_seconds": remaining,
                    "enabled_periodic_increase": previous == 0,
                }
        effective, reason, hold = await self.pump.read_many(
            [REG_EFFECTIVE, REG_REASON, REG_PI_HOLD], priority=Priority.COMMAND
        )
        raise CommandError(
            "trigger_failed",
            f"timer did not start in {POLL_TOTAL:.0f} s: R43109={effective} R40313={reason} R43014={hold}",
        )

    async def _after_start(self) -> None:
        """Refresh the periodic increase sensors right after a successful trigger."""
        try:
            timer = await self.pump.read_ram(*PI_TIMER, priority=Priority.COMMAND)
            enable, effective, hold = await self.pump.read_many(
                [REG_PI_ENABLE, REG_EFFECTIVE, REG_PI_HOLD], priority=Priority.COMMAND
            )
        except PumpError as exc:
            logger.warning("Could not refresh periodic increase status: %s", exc)
            return
        readings = {
            "pi_running": self._decode("pi_running", timer),
            "pi_remaining": self._decode("pi_remaining", timer),
            "pi_enabled": self._decode("pi_enabled", enable),
            "hw_mode": self._decode("hw_mode", effective),
            "pi_hold": self._decode("pi_hold", hold),
        }
        await self._publish_values({k: v for k, v in readings.items() if v is not None})
        await self.refresh_periodic_status()
