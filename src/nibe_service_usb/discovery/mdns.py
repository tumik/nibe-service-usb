"""mDNS / DNS-SD advertisement (see ``docs/discovery.md``)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_nibe-usb._tcp.local."
AUTH_SCHEME = "hmac-sha256"
_MAX_LABEL_BYTES = 63
_MAX_TXT_VALUE_BYTES = 100


def _truncate(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def sanitize_name(name: str) -> str:
    """Remove control characters and dots, collapse whitespace."""
    cleaned = re.sub(r"[\x00-\x1f\x7f.]", " ", name)
    return re.sub(r"\s+", " ", cleaned).strip()


def instance_name(name: str, server_id: str) -> str:
    """``<sanitised name>-<first 8 hex of server_id>``, at most 63 bytes."""
    suffix = "-" + re.sub(r"[^0-9a-f]", "", server_id.lower())[:8]
    base = _truncate(sanitize_name(name) or "nibe", _MAX_LABEL_BYTES - len(suffix)).rstrip()
    return (base or "nibe") + suffix


def service_name(name: str, server_id: str) -> str:
    return f"{instance_name(name, server_id)}.{SERVICE_TYPE}"


def txt_properties(*, server_id: str, version: str, proto: int, name: str, model: str = "") -> dict[str, str]:
    props = {
        "id": server_id,
        "proto": str(proto),
        "ver": version,
        "auth": AUTH_SCHEME,
        "name": _truncate(name, _MAX_TXT_VALUE_BYTES),
    }
    if model:
        props["model"] = _truncate(model, _MAX_TXT_VALUE_BYTES)
    return props


def get_local_ipv4s() -> list[str]:
    """All non-loopback, non link-local IPv4 addresses of this host."""
    found: list[str] = []
    try:
        import ifaddr

        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if isinstance(ip.ip, str) and ip.ip not in found:
                    found.append(ip.ip)
    except Exception:
        logger.debug("ifaddr enumeration failed", exc_info=True)
    if not found:
        # Fallback: the address the kernel would use to reach the outside world.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("192.0.2.1", 9))
                found.append(sock.getsockname()[0])
        except OSError:
            pass
    return [ip for ip in found if not (ip.startswith("127.") or ip.startswith("169.254.") or ip == "0.0.0.0")]


class MdnsAdvertiser:
    """Registers the service and keeps it correct when the host's addresses change.

    A background task compares the host's IPv4 addresses every ``check_interval``
    seconds. If they differ from what is advertised (DHCP renewal, Wi-Fi roam,
    cable plugged in), or if registration failed earlier, the zeroconf instance
    is recreated and the service registered again, so Home Assistant always
    finds the current address.
    """

    def __init__(
        self,
        config: Any,
        *,
        port: int,
        server_id: str,
        version: str,
        proto: int = 1,
        model: str = "",
        addresses: list[str] | None = None,
        zeroconf_factory: Callable[[], Any] | None = None,
        check_interval: float = 30.0,
    ) -> None:
        self._config = config
        self._port = port
        self._server_id = server_id
        self._version = version
        self._proto = proto
        self._model = model or getattr(config, "model", "")
        self._addresses = addresses
        self._factory = zeroconf_factory
        self._check_interval = check_interval
        self._azc: Any = None
        self._info: Any = None
        self._advertised: list[str] = []
        self._task: asyncio.Task | None = None
        self._enabled = getattr(config, "enabled", True)

    @property
    def instance_name(self) -> str:
        return instance_name(self._config.name, self._server_id)

    @property
    def properties(self) -> dict[str, str]:
        return txt_properties(
            server_id=self._server_id,
            version=self._version,
            proto=self._proto,
            name=self._config.name,
            model=self._model,
        )

    def _current_addresses(self) -> list[str]:
        return list(self._addresses) if self._addresses is not None else get_local_ipv4s()

    async def start(self) -> None:
        if not self._enabled:
            logger.info("mDNS discovery disabled")
            return
        await self._register()
        self._task = asyncio.create_task(self._watch(), name="mdns-watch")

    async def _register(self) -> None:
        try:
            from zeroconf import IPVersion
            from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

            addresses = self._current_addresses()
            if not addresses:
                logger.warning("mDNS: no usable IPv4 address found, not advertising (will retry)")
                return
            info = AsyncServiceInfo(
                SERVICE_TYPE,
                service_name(self._config.name, self._server_id),
                port=self._port,
                addresses=[socket.inet_aton(a) for a in addresses],
                properties=self.properties,
            )
            azc = self._factory() if self._factory else AsyncZeroconf(ip_version=IPVersion.V4Only)
            self._azc = azc
            await azc.async_register_service(info)
            self._info = info
            self._advertised = addresses
            logger.info("mDNS: advertising %s on %s port %d", info.name, ", ".join(addresses), self._port)
        except Exception as err:
            logger.warning("mDNS registration failed (will retry): %s", err)
            logger.debug("mDNS failure details", exc_info=True)
            await self._close()

    async def _deregister(self) -> None:
        try:
            if self._azc is not None and self._info is not None:
                await self._azc.async_unregister_service(self._info)
        except Exception as err:
            logger.warning("mDNS unregister failed: %s", err)
        finally:
            self._info = None
            self._advertised = []
            await self._close()

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self._check_interval)
            try:
                current = self._current_addresses()
                if self._info is None:
                    if current:
                        await self._register()
                elif set(current) != set(self._advertised):
                    logger.info(
                        "mDNS: addresses changed (%s -> %s), re-registering",
                        ", ".join(self._advertised) or "none",
                        ", ".join(current) or "none",
                    )
                    await self._deregister()
                    await self._register()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("mDNS address watch failed")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._deregister()

    async def _close(self) -> None:
        azc, self._azc = self._azc, None
        if azc is not None:
            try:
                await azc.async_close()
            except Exception as err:
                logger.warning("mDNS close failed: %s", err)
