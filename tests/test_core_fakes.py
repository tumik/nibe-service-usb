"""Shared fakes and helpers for the ``test_core_*`` modules (not a test module itself)."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from nibe_service_usb.config import Config, load_config
from nibe_service_usb.core.events import EventBus
from nibe_service_usb.core.state import State
from nibe_service_usb.pump.interface import (
    WRITABLE_RAM,
    WRITABLE_REGISTERS,
    Clock,
    Identity,
    Priority,
    PumpOffline,
    WriteFailed,
)
from nibe_service_usb.pump.registers import SensorDef, load_sensor_defs

DM_ADDRESS = 0x2038AF18
PI_TIMER_ADDRESS = 0x2066718C

DEFAULT_REGISTERS: dict[int, int] = {
    40004: -34,  # BT1 outdoor -3.4
    40008: 350,  # BT2 flow 35.0
    40033: 215,  # BT50 room 21.5 (not fitted on the F1226)
    40067: -20,
    43009: 340,
    40940: 0,  # degree minutes (raw)
    43086: 30,  # priority heating
    43110: 1,  # heat demand
    43427: 20,  # compressor idle
    43405: 0,  # inhibit
    47206: -200,  # start compressor (DM)
    43071: 1000,  # uptime
    40969: 96000,
    40965: 0,
    40967: 0,
    47041: 1,  # comfort normal
    43109: 1,  # effective normal
    40313: 0,
    43014: 1,
    47050: 1,
    32041: 25,
    47375: 180,
}


class FakePump:
    """In-memory PumpClient: registers, RAM, recorded writes, injectable errors."""

    def __init__(self, registers: dict[int, int] | None = None) -> None:
        self.registers: dict[int, int] = {**DEFAULT_REGISTERS, **(registers or {})}
        self.ram: dict[int, bytes] = {PI_TIMER_ADDRESS: bytes(3)}
        self.temperatures: list[int] = [0] * 28
        self.temperatures[0] = -34  # BT1
        self.temperatures[1] = 350  # BT2
        self.temperatures[2] = 300  # BT3
        self.temperatures[17 - 1] = 100  # BT17
        self.temperatures[25 - 1] = -32768  # BT25 not fitted
        self.dm_address = DM_ADDRESS  # where the RAM copy of R40940 really is
        self.writes: list[tuple] = []  # ("reg", register, value) / ("ram", address, data)
        self.calls: list[tuple] = []  # (method, args)
        self.error: Exception | None = None  # raised by every I/O method when set
        self.fail_registers: dict[int, Exception] = {}  # raised when a read touches the register
        self.fail_writes: bool = False
        self.write_hooks: list[Callable[[int, int, int], None]] = []
        self.identity_value = Identity(
            product="F1226-6 R",
            software_version="7994R7",
            brand=1,
            software_id=7994,
            base_version=25,
            product_code="06522820001001",
            article="065228",
            manufactured=date(2020, 1, 1),
            serial="001",
        )
        self.clock_local = datetime(2010, 1, 1, 22, 21, 45)
        self.clock_unixtime = 1262373705
        self.host_unixtime = 1_760_000_000.0
        self._online = True
        self._callbacks: list[Callable] = []

    # -- PumpClient properties
    @property
    def online(self) -> bool:
        return self._online

    @property
    def port(self) -> str:
        return "/dev/fake"

    @property
    def error_count(self) -> int:
        return 0

    @property
    def last_ok(self) -> float | None:
        return 1_760_000_000.0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    # -- helpers
    def go_offline(self) -> None:
        self._online = False

    async def set_online(self, online: bool) -> None:
        self._online = online
        for callback in self._callbacks:
            result = callback(online)
            if inspect.isawaitable(result):
                await result

    def on_state_change(self, callback: Callable) -> None:
        self._callbacks.append(callback)

    def _check(self, name: str, *args: object) -> None:
        self.calls.append((name, *args))
        if self.error is not None:
            raise self.error
        if not self._online:
            raise PumpOffline("offline")

    def registers_read(self) -> list[int]:
        """Every register touched by read/read_many, in order."""
        out: list[int] = []
        for name, *args in self.calls:
            if name == "read_many":
                out.extend(args[0])
            elif name == "read":
                out.append(args[0])
        return out

    # -- reads
    async def read(self, register: int, *, priority: Priority = Priority.POLL) -> int:
        return (await self.read_many([register], priority=priority))[0]

    async def read_many(self, registers: Sequence[int], *, priority: Priority = Priority.POLL) -> list[int]:
        self._check("read_many", list(registers))
        for register in registers:
            if register in self.fail_registers:
                raise self.fail_registers[register]
        return [self.registers.get(register, 0) for register in registers]

    async def read_temperatures(self, *, priority: Priority = Priority.POLL) -> list[int]:
        self._check("read_temperatures")
        return list(self.temperatures)

    async def read_ram(self, address: int, length: int, *, priority: Priority = Priority.POLL) -> bytes:
        self._check("read_ram", address, length)
        if address == self.dm_address:
            return self.registers.get(40940, 0).to_bytes(4, "little", signed=True)[:length]
        return self.ram.get(address, bytes(length))[:length]

    # -- writes
    async def write(self, register: int, value: int, *, priority: Priority = Priority.COMMAND) -> None:
        self._check("write", register, value)
        if (
            register not in WRITABLE_REGISTERS
            or value not in WRITABLE_REGISTERS[register]
            or self.fail_writes
        ):
            raise WriteFailed(f"write of {register} refused", readback=self.registers.get(register))
        old = self.registers.get(register, 0)
        self.writes.append(("reg", register, value))
        self.registers[register] = value
        for hook in self.write_hooks:
            hook(register, old, value)

    async def write_ram(self, address: int, data: bytes, *, priority: Priority = Priority.COMMAND) -> None:
        self._check("write_ram", address, data)
        if WRITABLE_RAM.get(address) != len(data) or self.fail_writes:
            raise WriteFailed(f"write to 0x{address:x} refused")
        self.writes.append(("ram", address, bytes(data)))
        if address == self.dm_address:
            self.registers[40940] = int.from_bytes(data, "little", signed=True)

    # -- identity / clock
    async def identity(self, *, priority: Priority = Priority.CONTROL) -> Identity:
        self._check("identity")
        return self.identity_value

    async def clock(self, *, priority: Priority = Priority.CONTROL) -> Clock:
        self._check("clock")
        return Clock(
            unixtime=self.clock_unixtime,
            local=self.clock_local,
            dls=0,
            tz="Helsinki",
            w_offset=2,
            s_offset=3,
            host_unixtime=self.host_unixtime,
        )


class Recorder:
    """Collects every bus event as ``(topic, data)``."""

    def __init__(self, bus: EventBus) -> None:
        self.events: list[tuple[str, dict]] = []
        bus.subscribe("*", self._on_event)

    def _on_event(self, topic: str, data: dict) -> None:
        self.events.append((topic, data))

    def of(self, topic: str) -> list[dict]:
        return [data for t, data in self.events if t == topic]

    def clear(self) -> None:
        self.events.clear()


def make_config(tmp_path: Path, **sections: dict) -> Config:
    """Default config with ``dataclasses.replace`` applied per section: ``heating={"dm_min": -5000}``."""
    cfg = load_config(None, data_dir=tmp_path)
    for name, values in sections.items():
        cfg = replace(cfg, **{name: replace(getattr(cfg, name), **values)})
    return cfg


class FakeClock:
    """Manually advanced clock."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_state(sensors: list[SensorDef] | None = None, clock: Callable[[], float] | None = None):
    bus = EventBus()
    kwargs = {} if clock is None else {"clock": clock}
    state = State(sensors if sensors is not None else load_sensor_defs(None), bus, "0.1.0", **kwargs)
    return state, bus, Recorder(bus)


async def wait_for(condition: Callable[[], bool], limit: float = 2.0) -> None:
    """Poll ``condition`` until true (real time, short steps)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)
