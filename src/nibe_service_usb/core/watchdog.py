"""HA liveness watchdog (``docs/security.md``)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from nibe_service_usb.config import Config
from nibe_service_usb.core.state import State

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 10.0


class Watchdog:
    """Reverts to the pump's own control after ``watchdog.timeout`` seconds without heartbeat."""

    def __init__(
        self,
        config: Config,
        state: State,
        on_timeout: Callable[[], Awaitable[None]],
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        check_interval: float = CHECK_INTERVAL,
    ) -> None:
        self.config = config
        self.state = state
        self.on_timeout = on_timeout
        self._clock = clock
        self._wall = wall
        self._check_interval = check_interval
        self._last = clock()
        self._task: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        state.watchdog.timeout = config.watchdog.timeout

    async def start(self) -> None:
        # HA is only "alive" once a heartbeat has been seen; a server that starts
        # without any HA connection must never revert (it would write the default
        # hot water mode onto a pump that nobody asked it to touch).
        self._last = self._clock()
        self.state.watchdog.ha_alive = False
        self._task = asyncio.create_task(self._run(), name="watchdog")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for bg in list(self._background):
            bg.cancel()

    def heartbeat(self) -> None:
        """Stamp the time; a heartbeat after a timeout marks HA alive again (modes are not restored)."""
        self._last = self._clock()
        status = self.state.watchdog
        status.last_heartbeat = self._wall()
        if not status.ha_alive:
            status.ha_alive = True
            if status.reverted_at is None:
                logger.info("First heartbeat received; HA is alive")
            else:
                logger.info("Heartbeat received again; HA is alive (modes are not restored)")
            try:
                task = asyncio.get_running_loop().create_task(self.state.status_changed())
            except RuntimeError:
                return
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def check(self) -> None:
        """One watchdog check (the background task calls this every 10 s)."""
        status = self.state.watchdog
        if not status.ha_alive or self._clock() - self._last <= self.config.watchdog.timeout:
            return
        status.ha_alive = False
        status.reverted_at = self._wall()
        logger.warning(
            "No heartbeat for %.0f s: reverting to automatic control", self.config.watchdog.timeout
        )
        try:
            await self.on_timeout()
        except Exception:
            logger.exception("Watchdog revert failed")
        await self.state.status_changed()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._check_interval)
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Watchdog check failed")
