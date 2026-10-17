"""Application wiring: builds every component, starts and stops them in order."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

from nibe_service_usb import __version__
from nibe_service_usb.api.commands import CommandContext
from nibe_service_usb.api.server import TcpServer
from nibe_service_usb.config import Config
from nibe_service_usb.core.events import EventBus
from nibe_service_usb.core.heating import HeatingController
from nibe_service_usb.core.hot_water import HotWaterController
from nibe_service_usb.core.poller import SensorPoller
from nibe_service_usb.core.state import State
from nibe_service_usb.core.watchdog import Watchdog
from nibe_service_usb.discovery.mdns import MdnsAdvertiser
from nibe_service_usb.keystore import KeyStore
from nibe_service_usb.pump.client import SerialPumpClient
from nibe_service_usb.pump.interface import PumpClient, PumpError, set_dm_ram_address
from nibe_service_usb.pump.registers import SensorDef, load_sensor_defs

logger = logging.getLogger(__name__)

PROBE_ONLINE_TIMEOUT = 10.0
PROBE_REGISTERS = (40004, 40008, 40940, 47041, 43109, 47206, 43071, 40969, 40965, 40967)
ANY_ADDRESS = ("0.0.0.0", "", "::")


class App:
    """Owns all components. ``start()``/``stop()`` for tests, ``run()`` for the CLI."""

    def __init__(self, config: Config, *, pump: PumpClient | None = None) -> None:
        self.config = config
        set_dm_ram_address(config.heating.dm_ram_address)
        self.keystore = KeyStore(config.data_dir)
        self.keystore.load_or_create()
        self.bus = EventBus()

        sensors: list[SensorDef] = load_sensor_defs(config.poll.sensors_file or None)
        if not config.poll.diagnostic:
            sensors = [s for s in sensors if s.tier != "diagnostic"]
        self.sensors = sensors

        self.state = State(sensors, self.bus, __version__)
        self.pump: PumpClient = pump if pump is not None else SerialPumpClient(config.pump)
        self.poller = SensorPoller(config, self.pump, self.state, sensors)
        self.heating = HeatingController(config, self.pump, self.state)
        self.hot_water = HotWaterController(config, self.pump, self.state, self.poller)
        self.watchdog = Watchdog(config, self.state, self._on_watchdog_timeout)
        self.ctx = CommandContext(
            config, self.state, self.pump, self.poller, self.heating, self.hot_water,
            self.watchdog, self.keystore, self.bus,
        )  # fmt: skip
        self.tcp = TcpServer(config, self.ctx)
        self.mdns: MdnsAdvertiser | None = None
        self.debug_web: Any = None

        # Poller restart / pump coming back online -> re-evaluate the heating request.
        self.poller.on_restart = self.heating.on_pump_online
        self.pump.on_state_change(self._on_pump_state)

        self._stack: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        self.ready = asyncio.Event()  # set once everything is started
        self._stop_event = asyncio.Event()
        self._stop_lock = asyncio.Lock()
        self._started = False
        self._stopped = False

    # ------------------------------------------------------------------ properties

    @property
    def tcp_port(self) -> int:
        return self.tcp.port

    @property
    def debug_url(self) -> str | None:
        return getattr(self.debug_web, "url", None) if self.debug_web is not None else None

    # ------------------------------------------------------------------ callbacks

    async def _on_pump_state(self, online: bool) -> None:
        if online:
            await self.heating.on_pump_online()

    async def _on_watchdog_timeout(self) -> None:
        """Home Assistant went silent: hand control back to the pump."""
        try:
            await self.heating.set_mode("auto")
        except Exception:
            logger.exception("Watchdog: reverting the heating request failed")
        mode = self.config.defaults.hot_water_mode
        if mode:
            try:
                await self.hot_water.set_mode(mode)
            except Exception:
                logger.exception("Watchdog: restoring hot water mode %s failed", mode)

    # ------------------------------------------------------------------ lifecycle

    def _build_optional(self) -> None:
        cfg = self.config
        if cfg.discovery.enabled:
            host = cfg.server.listen_host
            self.mdns = MdnsAdvertiser(
                cfg.discovery,
                port=self.tcp.port,
                server_id=self.keystore.server_id,
                version=__version__,
                addresses=[host] if host not in ANY_ADDRESS else None,
            )
        if cfg.debug_website.enabled:
            try:
                from nibe_service_usb.debug.web import DebugWebServer
            except ImportError:
                logger.warning("Debug website requested but nibe_service_usb.debug is not available")
            else:
                self.debug_web = DebugWebServer(cfg.debug_website, self.ctx, self.bus)

    async def start(self) -> None:
        """Start everything in order; on failure, stop what was started and re-raise."""
        if self._started:
            return
        self._started = True
        try:
            await self.pump.start()
            self._stack.append(("pump", self.pump.stop))
            await self.poller.start()
            self._stack.append(("poller", self.poller.stop))
            await self.heating.start()
            self._stack.append(("heating", self.heating.stop))
            await self.watchdog.start()
            self._stack.append(("watchdog", self.watchdog.stop))
            await self.tcp.start()
            self._stack.append(("tcp server", self.tcp.stop))
            self._build_optional()
            if self.mdns is not None:
                await self.mdns.start()
                self._stack.append(("mdns", self.mdns.stop))
            if self.debug_web is not None:
                await self.debug_web.start()
                self._stack.append(("debug web", self.debug_web.stop))
        except BaseException:
            await self.stop()
            raise
        self.ready.set()
        cfg = self.config.server
        logger.info("listening on %s:%d", cfg.listen_host, self.tcp.port)
        if self.debug_web is not None:
            logger.info("debug website at %s", self.debug_url)

    async def run(self) -> None:
        """Start, then block until :meth:`stop` is called."""
        await self.start()
        await self._stop_event.wait()
        await self.stop()  # waits for a teardown already in progress

    async def stop(self) -> None:
        """Stop in reverse start order. Idempotent; never writes to the pump."""
        self._stop_event.set()
        async with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            logger.info("shutting down")
            while self._stack:
                name, stopper = self._stack.pop()
                try:
                    await stopper()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Error stopping %s", name)


# ---------------------------------------------------------------------- probe


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


async def probe(config: Config) -> dict[str, Any]:
    """Open the pump, read identity, clock and a few registers, close it again."""
    pump = SerialPumpClient(config.pump)
    result: dict[str, Any] = {"port": config.pump.port, "online": False}
    came_online = asyncio.Event()
    pump.on_state_change(lambda online: came_online.set() if online else None)
    await pump.start()
    try:
        if not pump.online:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(came_online.wait(), PROBE_ONLINE_TIMEOUT)
        if not pump.online:
            result["error"] = f"pump did not come online within {PROBE_ONLINE_TIMEOUT:g} s"
            return result
        result["online"] = True
        for key, call in (("identity", pump.identity), ("clock", pump.clock)):
            try:
                result[key] = _jsonable(await call())
            except PumpError as exc:
                result[key] = {"error": str(exc)}
        registers: dict[str, Any] = {}
        for register in PROBE_REGISTERS:
            try:
                registers[str(register)] = await pump.read(register)
            except PumpError as exc:
                registers[str(register)] = f"error: {exc}"
        result["registers"] = registers
        return result
    finally:
        with contextlib.suppress(Exception):
            await pump.stop()
