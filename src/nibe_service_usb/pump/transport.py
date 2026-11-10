"""Serial transport: one CR LF terminated command, N CR LF terminated reply lines.

The transport knows nothing about replies. It enforces the verb allowlist and
the 61 character line limit before anything reaches the port. The caller (the
client) guarantees one request at a time; a lock guards it anyway.

Arming: once the host has raised RTS the console listens ("armed"). A USB bus
reset in that state (host reboot, cable replug) leaves the console deaf until
the pump is restarted. ``disarm()`` ends listening cleanly (RTS low, then one
CR LF that the pump ignores), so a bus reset is harmless afterwards.
``request()`` raises RTS again when needed. See ``docs/pump-interface.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol

from ..config import PumpConfig
from .codec import assert_allowed
from .interface import PumpError, PumpProtocolError, PumpTimeout

log = logging.getLogger(__name__)

CRLF = b"\r\n"
DISARM_SETTLE = 0.1  # seconds for the dummy packet to reach the pump before anything else happens


class TransportError(PumpError):
    """The port failed (not open, closed by the peer, I/O error)."""


class Transport(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def request(self, line: str, *, expect_lines: int = 1, timeout: float) -> list[bytes]:  # noqa: ASYNC109
        """Send ``line`` + CR LF and return ``expect_lines`` reply lines (terminator stripped)."""
        ...

    async def drain_input(self) -> None:
        """Discard stale bytes."""
        ...

    async def disarm(self) -> None:
        """Leave the pump console in a state that survives a USB bus reset. No-op when not armed."""
        ...

    @property
    def is_armed(self) -> bool:
        """True while the pump console is listening, i.e. a USB bus reset would hang it."""
        ...


class SerialTransport:
    def __init__(self, config: PumpConfig) -> None:
        self._config = config
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._armed = False

    @property
    def is_open(self) -> bool:
        return self._writer is not None

    @property
    def is_armed(self) -> bool:
        return self._armed

    def _serial(self) -> object | None:
        writer = self._writer
        return getattr(writer.transport, "serial", None) if writer is not None else None

    async def open(self) -> None:
        import serial_asyncio_fast  # imported late: the simulator and tests never need a port

        await self.close()
        cfg = self._config
        try:
            reader, writer = await serial_asyncio_fast.open_serial_connection(
                url=cfg.port, baudrate=cfg.baudrate
            )
        except (OSError, ValueError) as exc:
            raise TransportError(f"cannot open {cfg.port}: {exc}") from exc
        ser = getattr(writer.transport, "serial", None)
        if ser is not None:
            try:
                ser.rts = cfg.rts
                ser.dtr = cfg.dtr
            except (OSError, ValueError, AttributeError) as exc:
                log.warning("cannot set RTS/DTR on %s: %s", cfg.port, exc)
        self._reader, self._writer = reader, writer
        # The kernel raises RTS on open whatever we ask for, so the pump arms its read either way.
        self._armed = True
        log.info("opened %s (rts=%s dtr=%s)", cfg.port, cfg.rts, cfg.dtr)
        if cfg.rts and cfg.arm_delay > 0:
            await asyncio.sleep(cfg.arm_delay)

    async def _arm(self) -> None:
        """Raise RTS and give the pump's main loop time to start its read. Call with the lock held."""
        ser = self._serial()
        if ser is not None:
            try:
                ser.rts = True  # type: ignore[attr-defined]
            except (OSError, ValueError, AttributeError) as exc:
                raise TransportError(f"cannot raise RTS on {self._config.port}: {exc}") from exc
        self._armed = True
        log.debug("armed %s", self._config.port)
        if self._config.arm_delay > 0:
            await asyncio.sleep(self._config.arm_delay)

    async def _disarm_locked(self) -> None:
        """Lower RTS and send the dummy line. The port counts as disarmed only once both went out."""
        writer = self._writer
        if writer is None or not self._armed:
            return
        ser = self._serial()
        if ser is None:
            return  # no RTS control (not a serial port): the port stays armed
        try:
            ser.rts = False  # type: ignore[attr-defined]
        except (OSError, ValueError, AttributeError) as exc:
            # Without RTS control the dummy line would be read as a command; leave the port armed.
            log.debug("cannot lower RTS on %s, not disarming: %s", self._config.port, exc)
            return
        try:
            writer.write(CRLF)
            async with asyncio.timeout(1.0):
                await writer.drain()
            await asyncio.sleep(DISARM_SETTLE)
        except (OSError, TimeoutError) as exc:
            # The dummy line may not have arrived, so the port still counts as armed. Raise RTS
            # again for the next request; the next idle period retries the disarm.
            log.warning("disarm of %s failed: %s", self._config.port, exc)
            with contextlib.suppress(OSError, ValueError, AttributeError):
                ser.rts = self._config.rts  # type: ignore[attr-defined]
            return
        self._armed = False
        log.debug("disarmed %s", self._config.port)

    async def disarm(self) -> None:
        async with self._lock:
            await self._disarm_locked()

    async def close(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                async with self._lock:
                    await self._disarm_locked()
        self._armed = False
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            log.info("closed %s", self._config.port)

    async def drain_input(self, quiet: float = 0.05) -> None:
        """Read and drop everything until the line has been silent for ``quiet`` seconds."""
        reader = self._reader
        if reader is None:
            return
        dropped = 0
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), quiet)
            except TimeoutError:
                break
            if not chunk:
                break
            dropped += len(chunk)
        if dropped:
            log.debug("discarded %d stale bytes", dropped)

    async def request(self, line: str, *, expect_lines: int = 1, timeout: float) -> list[bytes]:  # noqa: ASYNC109
        assert_allowed(line, max_line_length=self._config.max_line_length)
        async with self._lock:
            reader, writer = self._reader, self._writer
            if reader is None or writer is None:
                raise TransportError("port is not open")
            if not self._armed and self._config.rts:
                await self._arm()
            payload = line.encode("ascii") + CRLF
            log.debug("> %r", payload)
            try:
                async with asyncio.timeout(timeout):
                    writer.write(payload)
                    await writer.drain()
                    lines = []
                    for _ in range(expect_lines):
                        raw = await reader.readuntil(CRLF)
                        lines.append(raw[: -len(CRLF)])
            except TimeoutError:
                log.debug("< (timeout after %.2fs) %r", timeout, line)
                raise PumpTimeout(f"no reply to {line!r} within {timeout:g}s") from None
            except asyncio.IncompleteReadError as exc:
                raise TransportError(f"port closed while reading the reply to {line!r}") from exc
            except asyncio.LimitOverrunError as exc:
                raise PumpProtocolError(f"reply to {line!r} has no line terminator") from exc
            except OSError as exc:
                raise TransportError(f"I/O error on {self._config.port}: {exc}") from exc
            log.debug("< %r", lines)
            return lines
