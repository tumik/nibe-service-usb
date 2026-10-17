"""Safety guards for the heating ``disable`` mode (see ``docs/heating-control.md``)."""

from __future__ import annotations

import logging

from nibe_service_usb.config import SafetyConfig
from nibe_service_usb.core.state import State

logger = logging.getLogger(__name__)

# (suspended reason, sensor name, SafetyConfig attribute holding the minimum)
GUARDS: tuple[tuple[str, str, str], ...] = (
    ("safety_room_temp", "bt50_room", "min_room_temp"),
    ("safety_supply_temp", "bt2_flow", "min_supply_temp"),
    ("safety_outdoor_temp", "bt1_outdoor", "min_outdoor_temp"),
)
GUARD_SENSORS: tuple[str, ...] = tuple(sensor for _, sensor, _ in GUARDS)


class Guards:
    """Temperature guards with hysteresis plus a stale-data guard.

    A tripped guard clears only once the value is above ``limit + hysteresis``.
    A guard whose limit is ``None`` is inactive.
    """

    def __init__(self, config: SafetyConfig) -> None:
        self.config = config
        self._tripped: dict[str, bool] = {reason: False for reason, _, _ in GUARDS}

    def active_sensors(self) -> list[str]:
        """Sensors that currently have a configured limit."""
        return [sensor for _, sensor, attr in GUARDS if getattr(self.config, attr) is not None]

    def reset(self) -> None:
        for reason in self._tripped:
            self._tripped[reason] = False

    def check(self, state: State) -> str | None:
        """Suspended reason, or None when heating may be held off."""
        stale = False
        for reason, sensor, attr in GUARDS:
            limit = getattr(self.config, attr)
            if limit is None:
                self._tripped[reason] = False
                continue
            value = state.get(sensor)
            age = state.sensor_age(sensor)
            if value is None or age is None or age > self.config.max_sensor_age:
                stale = True
                continue
            if self._tripped[reason]:
                if value > limit + self.config.hysteresis:
                    self._tripped[reason] = False
            elif value < limit:
                self._tripped[reason] = True
        for reason, _, _ in GUARDS:
            if self._tripped[reason]:
                return reason
        return "stale_data" if stale else None
