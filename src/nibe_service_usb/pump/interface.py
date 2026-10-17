"""Pump client interface: priorities, errors, records, write allowlist.

Pure definitions, no I/O. See ``docs/pump-interface.md``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import IntEnum
from typing import Protocol


class Priority(IntEnum):
    """Lower value = served first."""

    COMMAND = 0
    CONTROL = 1
    POLL = 2


class PumpError(Exception):
    """Base class for pump access errors."""


class PumpOffline(PumpError):
    """The pump is not reachable."""


class PumpTimeout(PumpError):
    """No (valid) reply within the request timeout."""


class PumpProtocolError(PumpError):
    """The reply did not match what the command allows."""


class WriteFailed(PumpError):
    """A write was refused or the read-back differs; ``readback`` is the value read, if any."""

    def __init__(self, message: str = "", readback: int | None = None) -> None:
        super().__init__(message)
        self.readback = readback


@dataclass(frozen=True)
class Identity:
    product: str
    software_version: str
    brand: int
    software_id: int
    base_version: int
    product_code: str
    article: str
    manufactured: date | None
    serial: str


@dataclass(frozen=True)
class Clock:
    unixtime: int
    local: datetime
    dls: int
    tz: str
    w_offset: int
    s_offset: int
    host_unixtime: float

    @property
    def drift(self) -> float:
        """Pump clock minus host clock, seconds."""
        return self.unixtime - self.host_unixtime

    def state(self, max_drift: float) -> str:
        """``"unset"`` (pump year < 2020), ``"ok"`` (|drift| <= max_drift) or ``"drifting"``."""
        if self.local.year < 2020:
            return "unset"
        return "ok" if abs(self.drift) <= max_drift else "drifting"


# Exact write allowlist enforced by the transport. Register -> allowed values.
WRITABLE_REGISTERS: dict[int, range] = {
    47041: range(0, 3),
    47050: range(0, 2),
    40940: range(-15000, 1),
}
# RAM address -> length in bytes.
DEFAULT_DM_RAM_ADDRESS = 0x2038AF18
WRITABLE_RAM: dict[int, int] = {DEFAULT_DM_RAM_ADDRESS: 4}  # address -> length; exactly one entry


def set_dm_ram_address(address: int) -> None:
    """Point the RAM write allowlist at the configured degree-minute address.

    The allowlist always holds exactly one 4-byte target (the RAM copy of R40940),
    so changing ``heating.dm_ram_address`` for other firmware moves it rather than
    adding a second writable location.
    """
    WRITABLE_RAM.clear()
    WRITABLE_RAM[address] = 4


class PumpClient(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    @property
    def online(self) -> bool: ...

    @property
    def port(self) -> str: ...

    @property
    def error_count(self) -> int: ...

    @property
    def last_ok(self) -> float | None: ...

    async def read(self, register: int, *, priority: Priority = Priority.POLL) -> int: ...

    async def read_many(self, registers: Sequence[int], *, priority: Priority = Priority.POLL) -> list[int]:
        """Any number of registers; chunked into lines of ``ri_batch_size`` internally."""
        ...

    async def read_temperatures(self, *, priority: Priority = Priority.POLL) -> list[int]:
        """``RT 1``: 28 raw values."""
        ...

    async def read_ram(self, address: int, length: int, *, priority: Priority = Priority.POLL) -> bytes: ...

    async def write(self, register: int, value: int, *, priority: Priority = Priority.COMMAND) -> None:
        """``WI`` plus read-back; raises ``WriteFailed``."""
        ...

    async def write_ram(self, address: int, data: bytes, *, priority: Priority = Priority.COMMAND) -> None:
        """``WM``; raises ``WriteFailed``."""
        ...

    async def identity(self, *, priority: Priority = Priority.CONTROL) -> Identity: ...

    async def clock(self, *, priority: Priority = Priority.CONTROL) -> Clock: ...

    def on_state_change(self, callback: Callable[[bool], Awaitable[None] | None]) -> None:
        """Register a callback invoked with ``True`` (online) / ``False`` (offline)."""
        ...
