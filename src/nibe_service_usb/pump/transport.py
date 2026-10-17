"""Serial transport: one CR LF terminated command, N CR LF terminated reply lines.

The transport knows nothing about replies. It enforces the verb allowlist and
the 61 character line limit before anything reaches the port. The caller (the
client) guarantees one request at a time; a lock guards it anyway.
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


class SerialTransport:
    def __init__(self, config: PumpConfig) -> None:
        self._config = config
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return self._writer is not None

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
        log.info("opened %s (rts=%s dtr=%s)", cfg.port, cfg.rts, cfg.dtr)

    async def close(self) -> None:
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
