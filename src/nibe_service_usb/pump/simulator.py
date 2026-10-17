"""``SimulatedPumpClient``: an in-memory F1226 for tests and ``--simulate``.

No serial code. Values are seeded with plausible F1226 values. Simulated time moves
only through :meth:`advance` (plain synchronous method, deterministic), or, with
``realtime=True``, additionally once per real second from a background task
started by :meth:`start`.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import math
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime, timedelta

from ..config import PumpConfig
from . import codec
from .interface import (
    WRITABLE_RAM,
    WRITABLE_REGISTERS,
    Clock,
    Identity,
    Priority,
    PumpOffline,
    PumpTimeout,
    WriteFailed,
)
from .registers import SensorDef

DM_ADDRESS = 0x2038AF18
PI_TIMER_ADDRESS = 0x2066718C
PI_DURATION = 5 * 3600
PI_TRIGGER_WINDOW = 30.0
RT_FIELDS = 28
NOT_FITTED = -32768
CLOCK_EPOCH = datetime(2010, 1, 1)
CLOCK_EPOCH_UNIX = 1262304000

SEED_REGISTERS: dict[int, int] = {
    40004: 47,
    40008: 262,
    40012: 227,
    40014: 480,
    40013: 529,
    40015: 207,
    40016: 173,
    40017: 237,
    40018: 312,
    40019: 236,
    40022: 269,
    47041: 1,
    43109: 1,
    40313: 0,
    47206: -200,
    47375: 180,
    43110: 0,
    43427: 20,
    43443: 5,
    43086: 10,
    43437: 0,
    43439: 0,
    43516: 0,
    43084: 0,
    43012: 0,
    32041: 25,
    43416: 5,
    43420: 10,
    43424: 0,
    43071: 82791,
    40969: 96859,
    40965: 0,
    40967: 0,
    47050: 1,
    43014: 1,
    47137: 0,
    43151: 1,
    40071: NOT_FITTED,  # BT25 is not fitted on this unit (RT field 25 reads -32768)
}
# RT 1 fields that are not mapped to a register by the sensor definitions.
RT_NOT_FITTED_FIELDS = (18, 19, 20, 21, 25)
RT_COPY_FIELDS = {13: 3, 16: 14}  # field -> field it copies


class SimulatedPumpClient:
    def __init__(
        self,
        sensors: Sequence[SensorDef] = (),
        *,
        latency: float = 0.0,
        fail_rate: float = 0.0,
        realtime: bool = False,
        seed: int | None = None,
        config: PumpConfig | None = None,
    ) -> None:
        self._sensors = list(sensors)
        self.latency = latency
        self.fail_rate = fail_rate
        self._realtime = realtime
        self._rng = random.Random(seed)
        self._config = config or PumpConfig(port="simulated")
        self.registers: dict[int, int] = {}
        for sensor in self._sensors:
            if sensor.register is not None:
                self.registers[sensor.register] = 0
        self.registers.update(SEED_REGISTERS)
        self._callbacks: list[Callable[[bool], Awaitable[None] | None]] = []
        self._pending: set[asyncio.Task[None]] = set()
        self._plugged = True
        self._stopped = False
        self._errors = 0
        self._last_ok: float | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._time = 0.0  # simulated seconds since construction
        self._minute_acc = 0.0
        self._run_acc = 0.0
        self._pi_left = 0.0  # seconds left of the periodic increase, 0 = not running
        self._pi_zero_at: float | None = None
        self._uptime_frac = 0.0

    # -- test helpers ---------------------------------------------------------

    def set_register(self, register: int, value: int) -> None:
        self.registers[register] = value

    @property
    def sim_time(self) -> float:
        return self._time

    @property
    def periodic_timer(self) -> int:
        """Seconds left of the periodic increase run (0 = not running)."""
        return math.ceil(self._pi_left)

    def unplug(self) -> None:
        if not self._plugged:
            return
        self._plugged = False
        self._notify(False)

    def plug(self) -> None:
        if self._plugged:
            return
        self._plugged = True
        self._notify(True)

    def _notify(self, online: bool) -> None:
        if self._stopped:
            return
        for cb in list(self._callbacks):
            result = cb(online)
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)

    # -- lifecycle / properties ---------------------------------------------------

    async def start(self) -> None:
        self._stopped = False
        if self._realtime and self._ticker is None:
            self._ticker = asyncio.create_task(self._tick_loop(), name="pump-sim-ticker")

    async def stop(self) -> None:
        self._stopped = True
        if self._ticker is not None:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
            self._ticker = None

    async def _tick_loop(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            self.advance(now - last)
            last = now

    @property
    def online(self) -> bool:
        return self._plugged and not self._stopped

    @property
    def port(self) -> str:
        return self._config.port

    @property
    def error_count(self) -> int:
        return self._errors

    @property
    def last_ok(self) -> float | None:
        return self._last_ok

    def on_state_change(self, callback: Callable[[bool], Awaitable[None] | None]) -> None:
        self._callbacks.append(callback)

    async def _access(self) -> None:
        """Common prologue of every request: offline check, latency, failure injection."""
        if not self.online:
            raise PumpOffline("simulated pump is offline")
        if self.latency > 0:
            await asyncio.sleep(self.latency)
            if not self.online:
                raise PumpOffline("simulated pump is offline")
        if self.fail_rate > 0 and self._rng.random() < self.fail_rate:
            self._errors += 1
            raise PumpTimeout("simulated timeout")
        self._last_ok = time.time()

    # -- reads ------------------------------------------------------------------

    async def read(self, register: int, *, priority: Priority = Priority.POLL) -> int:
        return (await self._ri([register]))[0]

    async def read_many(self, registers: Sequence[int], *, priority: Priority = Priority.POLL) -> list[int]:
        batch = max(1, min(self._config.ri_batch_size, codec.MAX_RI_REGISTERS))
        out: list[int] = []
        regs = list(registers)
        for i in range(0, len(regs), batch):
            out.extend(await self._ri(regs[i : i + batch]))
        return out

    async def _ri(self, registers: list[int]) -> list[int]:
        await self._access()
        return [self.registers.get(r, 0) for r in registers]

    async def read_temperatures(self, *, priority: Priority = Priority.POLL) -> list[int]:
        await self._access()
        fields = [0] * RT_FIELDS
        for n in RT_NOT_FITTED_FIELDS:
            fields[n - 1] = NOT_FITTED
        for sensor in self._sensors:
            if sensor.rt_field is not None and sensor.register is not None:
                fields[sensor.rt_field - 1] = self.registers.get(sensor.register, 0)
        for field, source in RT_COPY_FIELDS.items():
            fields[field - 1] = fields[source - 1]
        return fields

    async def read_ram(self, address: int, length: int, *, priority: Priority = Priority.POLL) -> bytes:
        await self._access()
        return self._ram_read(address, length)

    def _ram_read(self, address: int, length: int) -> bytes:
        if address == DM_ADDRESS and length == 4:
            return codec.encode_s32_le(self.registers.get(40940, 0))
        if address == PI_TIMER_ADDRESS and length == 3:
            t = self.periodic_timer
            return bytes((t // 3600, (t % 3600) // 60, t % 60))
        return bytes(length)

    # -- writes -----------------------------------------------------------------

    async def write(self, register: int, value: int, *, priority: Priority = Priority.COMMAND) -> None:
        allowed = WRITABLE_REGISTERS.get(register)
        if allowed is None or value not in allowed:
            raise ValueError(f"register {register} = {value} is not on the write allowlist")
        await self._access()
        self.registers[register] = value
        self.registers[40969] = self.registers.get(40969, 0) + 1  # one EEPROM store call
        if register == 47041 and self._pi_left <= 0:
            self.registers[43109] = value
        elif register == 47050:
            self._on_periodic_enable(value)
        readback = self.registers[register]
        if readback != value:  # pragma: no cover - cannot happen in memory
            raise WriteFailed("read-back mismatch", readback=readback)

    async def write_ram(self, address: int, data: bytes, *, priority: Priority = Priority.COMMAND) -> None:
        data = bytes(data)
        if WRITABLE_RAM.get(address) != len(data):
            raise ValueError(f"RAM write {address:#x} ({len(data)} bytes) is not on the write allowlist")
        if address == DM_ADDRESS:
            value = codec.decode_s32_le(data)
            if value not in WRITABLE_REGISTERS[40940]:
                raise ValueError(f"degree minutes {value} outside the allowed range")
        await self._access()
        if address == DM_ADDRESS:
            self.registers[40940] = codec.decode_s32_le(data)  # no EEPROM store call

    def _on_periodic_enable(self, value: int) -> None:
        if value == 0:
            self._pi_zero_at = self._time
            return
        started = (
            self._pi_zero_at is not None
            and self._time - self._pi_zero_at <= PI_TRIGGER_WINDOW
            and self._pi_left <= 0
        )
        self._pi_zero_at = None
        if started:
            self._pi_left = float(PI_DURATION)
            self.registers[43109] = 3
            self.registers[40313] = 3
            self.registers[43014] = 0

    # -- identity / clock ---------------------------------------------------------

    async def identity(self, *, priority: Priority = Priority.CONTROL) -> Identity:
        await self._access()
        return Identity(
            product="F1226-6 R",
            software_version="7994R7",
            brand=1,
            software_id=7994,
            base_version=self.registers.get(32041, 25),
            product_code="06522820001001",
            article="065228",
            manufactured=date(2020, 1, 1),
            serial="001",
        )

    async def clock(self, *, priority: Priority = Priority.CONTROL) -> Clock:
        await self._access()
        uptime = self.registers.get(43071, 0)
        return Clock(
            unixtime=CLOCK_EPOCH_UNIX + uptime - 3 * 3600,
            local=CLOCK_EPOCH + timedelta(seconds=uptime),
            dls=0,
            tz="Helsinki",
            w_offset=2,
            s_offset=3,
            host_unixtime=time.time(),
        )

    # -- simulated time -------------------------------------------------------------

    def advance(self, seconds: float) -> None:
        """Move simulated time forward. Synchronous; no real time passes."""
        remaining = float(seconds)
        while remaining > 0:
            step = min(remaining, 60.0 - self._minute_acc)
            remaining -= step
            self._step(step)
            self._minute_acc += step
            if self._minute_acc >= 60.0 - 1e-9:
                self._minute_acc = 0.0
                self._minute_tick()

    def _step(self, dt: float) -> None:
        self._time += dt
        regs = self.registers
        self._uptime_frac += dt
        whole = int(self._uptime_frac)
        self._uptime_frac -= whole
        regs[43071] = regs.get(43071, 0) + whole
        if self._pi_left > 0:
            self._pi_left = max(0.0, self._pi_left - dt)
            if self._pi_left <= 1e-9:
                self._pi_left = 0.0
                regs[43109] = regs.get(47041, 1)
                regs[40313] = 0
                regs[43014] = 1
        if regs.get(43427) == 60:
            self._run_acc += dt
            while self._run_acc >= 3600:
                self._run_acc -= 3600
                regs[43420] = regs.get(43420, 0) + 1

    def _minute_tick(self) -> None:
        regs = self.registers
        if regs.get(43110, 0) == 1:
            regs[40940] = regs.get(40940, 0) + regs.get(40008, 0) - regs.get(43009, 0)
        else:
            regs[40940] = 0
        threshold = regs.get(47206, 0) * 10
        dm = regs[40940]
        state = regs.get(43427, 20)
        if state == 20 and dm < threshold:
            state = 40
            regs[43416] = regs.get(43416, 0) + 1
        elif state == 40:
            state = 60
        elif state == 60 and dm >= 0:
            state = 100
        elif state == 100:
            state = 20
        regs[43427] = state
        regs[43435] = 1 if state in (40, 60) else 0
        regs[43443] = 6 if state == 60 else 5
