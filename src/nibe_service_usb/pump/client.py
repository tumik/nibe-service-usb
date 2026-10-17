"""``SerialPumpClient``: the real pump, one request in flight, resync, offline handling.

See ``docs/pump-interface.md`` (reply validation, write handling, offline
detection). All access goes through one worker task draining a priority queue,
so at most one request owns the port at any time and COMMAND requests overtake
CONTROL and POLL ones.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from ..config import PumpConfig
from . import codec
from .interface import (
    WRITABLE_RAM,
    WRITABLE_REGISTERS,
    Clock,
    Identity,
    Priority,
    PumpError,
    PumpOffline,
    PumpProtocolError,
    PumpTimeout,
    WriteFailed,
)
from .transport import SerialTransport, Transport, TransportError

log = logging.getLogger(__name__)

T = TypeVar("T")

RT_FIELDS = 28
RESYNC_TRIES = 3
RECONNECT_INTERVAL = 5.0
RECONNECT_MAX = 30.0

TransportFactory = Callable[[PumpConfig], Transport]
Parser = Callable[[list[bytes]], T]


@dataclass
class _Request:
    op: Callable[[], Awaitable[Any]]
    future: asyncio.Future[Any]


class SerialPumpClient:
    def __init__(
        self,
        config: PumpConfig,
        *,
        transport_factory: TransportFactory | None = None,
        reconnect_interval: float = RECONNECT_INTERVAL,
        reconnect_max: float = RECONNECT_MAX,
    ) -> None:
        self._config = config
        self._factory: TransportFactory = transport_factory or SerialTransport
        self._reconnect_interval = reconnect_interval
        self._reconnect_max = reconnect_max
        self._transport: Transport | None = None
        self._queue: asyncio.PriorityQueue[tuple[int, int, _Request]] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._worker: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._callbacks: list[Callable[[bool], Awaitable[None] | None]] = []
        self._online = False
        self._errors = 0
        self._last_ok: float | None = None
        self._last_ok_mono: float = time.monotonic()

    # -- properties ---------------------------------------------------------

    @property
    def online(self) -> bool:
        return self._online

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

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Open the port and verify the pump with ``b``.

        Never raises for an unreachable pump: the client then starts offline and
        the reconnect loop takes over. State change callbacks fire for later
        transitions only, not for the outcome of ``start()`` itself.
        """
        if self._worker is not None:
            return
        self._transport = self._factory(self._config)
        self._worker = asyncio.create_task(self._worker_loop(), name="pump-worker")
        try:
            await self._connect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("pump not reachable on %s: %s", self._config.port, exc)
            self._start_reconnect()
        else:
            self._online = True
            log.info("pump online on %s", self._config.port)

    async def stop(self) -> None:
        tasks = [t for t in (self._worker, self._reconnect_task) if t is not None]
        self._worker = None
        self._reconnect_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._online = False
        self._fail_pending(PumpOffline("pump client stopped"))
        if self._transport is not None:
            with contextlib.suppress(Exception):
                await self._transport.close()

    async def _connect(self) -> None:
        """Open the port, flush stale bytes and require a valid ``b`` reply."""
        assert self._transport is not None
        await self._transport.close()
        await self._transport.open()
        await self._transport.drain_input()
        lines = await self._transport.request(codec.build_b(), timeout=self._config.request_timeout)
        codec.parse_b(lines[0])
        self._mark_ok()

    # -- online / offline ---------------------------------------------------

    def _mark_ok(self) -> None:
        self._last_ok = time.time()
        self._last_ok_mono = time.monotonic()

    async def _fire(self, online: bool) -> None:
        for cb in list(self._callbacks):
            try:
                result = cb(online)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("state change callback failed")

    def _fail_pending(self, exc: PumpError) -> None:
        while True:
            try:
                _, _, req = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not req.future.done():
                req.future.set_exception(exc)

    def _start_reconnect(self) -> None:
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop(), name="pump-reconnect")

    async def _go_offline(self, reason: str) -> None:
        if not self._online and self._reconnect_task is not None:
            return
        was_online = self._online
        self._online = False
        log.warning("pump offline: %s", reason)
        self._fail_pending(PumpOffline(reason))
        self._start_reconnect()
        if was_online:
            await self._fire(False)

    async def _reconnect_loop(self) -> None:
        delay = self._reconnect_interval
        while True:
            await asyncio.sleep(delay)
            try:
                await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("reconnect failed: %s", exc)
                delay = min(delay * 2, self._reconnect_max)
                continue
            break
        self._reconnect_task = None
        self._online = True
        log.info("pump online again on %s", self._config.port)
        await self._fire(True)

    def _offline_due(self) -> bool:
        return time.monotonic() - self._last_ok_mono >= self._config.offline_after

    # -- queue --------------------------------------------------------------

    async def _worker_loop(self) -> None:
        while True:
            _, _, req = await self._queue.get()
            if req.future.done():  # caller gave up while queued
                continue
            try:
                result = await req.op()
            except asyncio.CancelledError:
                if not req.future.done():
                    req.future.set_exception(PumpOffline("pump client stopped"))
                raise
            except BaseException as exc:
                if not req.future.done():
                    req.future.set_exception(exc)
            else:
                if not req.future.done():
                    req.future.set_result(result)

    async def _submit(self, priority: Priority, op: Callable[[], Awaitable[T]]) -> T:
        if not self._online or self._worker is None:
            raise PumpOffline("pump is offline")
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((int(priority), next(self._seq), _Request(op, future)))
        return await future

    # -- exchange with resync -----------------------------------------------

    async def _exchange(
        self,
        line: str,
        parse: Callable[[list[bytes]], T],
        *,
        expect_lines: int = 1,
        attempts: int | None = None,
    ) -> T:
        """Send ``line``, parse the reply; on timeout or invalid reply resync and retry.

        Runs inside the worker only. Raises ``PumpTimeout`` / ``PumpProtocolError``
        when the attempts are used up, ``PumpOffline`` once the pump is declared offline.
        """
        assert self._transport is not None
        total = attempts if attempts is not None else 1 + self._config.retries
        last: PumpError | None = None
        for attempt in range(1, total + 1):
            try:
                lines = await self._transport.request(
                    line, expect_lines=expect_lines, timeout=self._config.request_timeout
                )
                result = parse(lines)
            except TransportError as exc:
                await self._go_offline(str(exc))
                raise PumpOffline(str(exc)) from exc
            except (PumpTimeout, PumpProtocolError) as exc:
                self._errors += 1
                last = exc
                log.warning("%r failed (attempt %d/%d): %s", line, attempt, total, exc)
            else:
                self._mark_ok()
                return result
            if not await self._resync():
                await self._declare_offline_if_due()
                break
        assert last is not None
        raise last

    async def _declare_offline_if_due(self) -> None:
        if self._offline_due():
            reason = f"no valid reply for {self._config.offline_after:g}s"
            await self._go_offline(reason)
            raise PumpOffline(reason)

    async def _resync(self) -> bool:
        """Discard stale replies: send ``RV`` until a valid ``RV`` reply arrives (max 3 tries)."""
        assert self._transport is not None
        try:
            await self._transport.drain_input()
        except (TransportError, OSError) as exc:
            await self._go_offline(str(exc))
            raise PumpOffline(str(exc)) from exc
        for n in range(1, RESYNC_TRIES + 1):
            try:
                lines = await self._transport.request(codec.build_rv(), timeout=self._config.request_timeout)
                codec.parse_rv(lines[0])
            except TransportError as exc:
                await self._go_offline(str(exc))
                raise PumpOffline(str(exc)) from exc
            except (PumpTimeout, PumpProtocolError) as exc:
                self._errors += 1
                log.warning("resync %d/%d: no valid RV reply (%s)", n, RESYNC_TRIES, exc)
                continue
            self._mark_ok()
            log.info("resynchronised after %d RV", n)
            return True
        return False

    # -- reads --------------------------------------------------------------

    async def read(self, register: int, *, priority: Priority = Priority.POLL) -> int:
        return (await self._read_chunk([register], priority))[0]

    async def read_many(self, registers: Sequence[int], *, priority: Priority = Priority.POLL) -> list[int]:
        out: list[int] = []
        for chunk in self._chunks(registers):
            out.extend(await self._read_chunk(chunk, priority))
        return out

    def _chunks(self, registers: Sequence[int]) -> list[list[int]]:
        cfg = self._config
        limit = min(cfg.ri_batch_size, codec.MAX_RI_REGISTERS)
        chunks: list[list[int]] = []
        current: list[int] = []
        for reg in registers:
            candidate = [*current, reg]
            fits = len(candidate) <= limit
            if fits:
                try:
                    codec.build_ri(candidate, max_line_length=cfg.max_line_length)
                except ValueError:
                    if not current:
                        raise
                    fits = False
            if fits:
                current = candidate
            else:
                chunks.append(current)
                current = [reg]
                codec.build_ri(current, max_line_length=cfg.max_line_length)  # validates a lone register
        if current:
            chunks.append(current)
        return chunks

    async def _read_chunk(self, chunk: list[int], priority: Priority) -> list[int]:
        line = codec.build_ri(chunk, max_line_length=self._config.max_line_length)
        count = len(chunk)

        async def op() -> list[int]:
            return await self._exchange(line, lambda lines: codec.parse_ri(lines[0], count))

        return await self._submit(priority, op)

    async def read_temperatures(self, *, priority: Priority = Priority.POLL) -> list[int]:
        def parse(lines: list[bytes]) -> list[int]:
            values = codec.parse_rt(lines[0])
            if len(values) != RT_FIELDS:
                raise PumpProtocolError(f"RT reply has {len(values)} fields, expected {RT_FIELDS}")
            return values

        async def op() -> list[int]:
            return await self._exchange(codec.build_rt(), parse)

        return await self._submit(priority, op)

    async def read_ram(self, address: int, length: int, *, priority: Priority = Priority.POLL) -> bytes:
        line = codec.build_rm(address, length)

        async def op() -> bytes:
            return await self._exchange(line, lambda lines: codec.parse_rm(lines[0], length))

        return await self._submit(priority, op)

    # -- writes -------------------------------------------------------------

    async def write(self, register: int, value: int, *, priority: Priority = Priority.COMMAND) -> None:
        allowed = WRITABLE_REGISTERS.get(register)
        if allowed is None or value not in allowed:
            raise ValueError(f"register {register} = {value} is not on the write allowlist")
        line = codec.build_wi(register, value)
        codec.assert_allowed(line, max_line_length=self._config.max_line_length)

        async def op() -> None:
            await self._acked_write(line, f"WI {register} {value}")
            try:
                readback = await self._exchange(
                    codec.build_ri([register], max_line_length=self._config.max_line_length),
                    lambda lines: codec.parse_ri(lines[0], 1)[0],
                )
            except (PumpTimeout, PumpProtocolError) as exc:
                raise WriteFailed(f"read-back of {register} failed: {exc}") from exc
            if readback != value:
                raise WriteFailed(f"WI {register} {value}: read back {readback}", readback=readback)

        await self._submit(priority, op)

    async def write_ram(self, address: int, data: bytes, *, priority: Priority = Priority.COMMAND) -> None:
        data = bytes(data)
        if WRITABLE_RAM.get(address) != len(data):
            raise ValueError(f"RAM write {address:#x} ({len(data)} bytes) is not on the write allowlist")
        if len(data) == 4:  # the only RAM target is the degree-minute copy
            dm = codec.decode_s32_le(data)
            if dm not in WRITABLE_REGISTERS[40940]:
                raise ValueError(f"degree minutes {dm} outside the allowed range")
        line = codec.build_wm(address, data)
        codec.assert_allowed(line, max_line_length=self._config.max_line_length)
        length = len(data)

        async def op() -> None:
            await self._acked_write(line, f"WM {address:#x} {data.hex()}")
            try:
                readback = await self._exchange(
                    codec.build_rm(address, length), lambda lines: codec.parse_rm(lines[0], length)
                )
            except (PumpTimeout, PumpProtocolError) as exc:
                raise WriteFailed(f"read-back of {address:#x} failed: {exc}") from exc
            if readback != data:
                value = codec.decode_s32_le(readback) if length == 4 else None
                raise WriteFailed(f"WM {address:#x} {data.hex()}: read back {readback.hex()}", readback=value)

        await self._submit(priority, op)

    async def _acked_write(self, line: str, what: str) -> None:
        """Send a write and require ``1``. No valid ack: one resync, one retry, then ``WriteFailed``."""
        try:
            await self._exchange(line, lambda lines: codec.parse_ack(lines[0]), attempts=2)
        except (PumpTimeout, PumpProtocolError) as exc:
            raise WriteFailed(f"{what} not acknowledged: {exc}") from exc

    # -- identity / clock ---------------------------------------------------

    async def identity(self, *, priority: Priority = Priority.CONTROL) -> Identity:
        async def op() -> Identity:
            brand, version, product = await self._exchange(
                codec.build_b(), lambda lines: codec.parse_b(lines[0])
            )
            software_id, base_version, _ = await self._exchange(
                codec.build_rv(), lambda lines: codec.parse_rv(lines[0])
            )
            code, article, manufactured, serial = await self._exchange(
                codec.build_qr(), lambda lines: codec.parse_qr(lines[0])
            )
            return Identity(
                product=product,
                software_version=version,
                brand=brand,
                software_id=software_id,
                base_version=base_version,
                product_code=code,
                article=article,
                manufactured=manufactured,
                serial=serial,
            )

        return await self._submit(priority, op)

    async def clock(self, *, priority: Priority = Priority.CONTROL) -> Clock:
        async def op() -> Clock:
            return await self._exchange(
                codec.build_ai(),
                lambda lines: codec.parse_ai(lines, time.time()),
                expect_lines=5,
            )

        return await self._submit(priority, op)
