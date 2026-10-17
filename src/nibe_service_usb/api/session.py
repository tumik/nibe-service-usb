"""One client connection."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .framing import encode_message

logger = logging.getLogger(__name__)

SEND_TIMEOUT = 10.0


class Session:
    """State of one TCP connection.

    ``subscriptions`` is ``None`` when not subscribed, ``{"*"}`` for all
    sensors, otherwise the set of sensor names.
    """

    def __init__(
        self,
        id: int,
        peer: str,
        writer: asyncio.StreamWriter | None = None,
        *,
        send_timeout: float = SEND_TIMEOUT,
    ) -> None:
        self.id = id
        self.peer = peer
        self.authenticated = False
        self.client = ""
        self.subscriptions: set[str] | None = None
        self.created = time.time()
        self.last_activity = self.created
        self.dead = False
        self._writer = writer
        self._send_timeout = send_timeout
        self._lock = asyncio.Lock()

    def touch(self) -> None:
        self.last_activity = time.time()

    def wants_sensor(self, name: str) -> bool:
        subs = self.subscriptions
        if not subs:
            return False
        return "*" in subs or name in subs

    async def send(self, obj: dict) -> None:
        """Write one JSON line. A slow or broken consumer is disconnected."""
        if self.dead:
            return
        if self._writer is None:
            return
        data = encode_message(obj)
        try:
            async with self._lock:
                if self.dead:
                    return
                self._writer.write(data)
                await asyncio.wait_for(self._writer.drain(), self._send_timeout)
        except TimeoutError:
            logger.warning("Client %s too slow, closing", self.peer)
            self.close()
        except (ConnectionError, OSError, RuntimeError):
            self.close()

    def close(self) -> None:
        """Mark the session dead and close the transport (idempotent)."""
        self.dead = True
        writer, self._writer = self._writer, None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
