import asyncio
import logging
import socket
from types import SimpleNamespace

import pytest

from nibe_service_usb.config import DiscoveryConfig
from nibe_service_usb.discovery import mdns
from nibe_service_usb.discovery.mdns import (
    SERVICE_TYPE,
    MdnsAdvertiser,
    get_local_ipv4s,
    instance_name,
    sanitize_name,
    service_name,
    txt_properties,
)

SERVER_ID = "6f1c2a9e-1234-4000-8000-000000000000"


def test_service_type():
    assert SERVICE_TYPE == "_nibe-usb._tcp.local."


def test_instance_name():
    assert instance_name("Garage pump", SERVER_ID) == "Garage pump-6f1c2a9e"
    assert service_name("Garage pump", SERVER_ID) == "Garage pump-6f1c2a9e._nibe-usb._tcp.local."


@pytest.mark.parametrize(
    "raw, clean",
    [
        ("a.b.c", "a b c"),
        ("  lots   of\tspace\n", "lots of space"),
        ("ctl\x00char", "ctl char"),
        ("", ""),
        ("...", ""),
    ],
)
def test_sanitize_name(raw, clean):
    assert sanitize_name(raw) == clean


def test_empty_name_falls_back():
    assert instance_name("...", SERVER_ID) == "nibe-6f1c2a9e"


def test_long_name_is_truncated_to_dns_label_limit():
    name = instance_name("é" * 100, SERVER_ID)
    assert len(name.encode()) <= 63
    assert name.endswith("-6f1c2a9e")


def test_txt_properties_exact_keys():
    props = txt_properties(server_id=SERVER_ID, version="0.1.0", proto=1, name="Garage pump", model="F1245")
    assert props == {
        "id": SERVER_ID,
        "proto": "1",
        "ver": "0.1.0",
        "auth": "hmac-sha256",
        "name": "Garage pump",
        "model": "F1245",
    }
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in props.items())


def test_txt_model_omitted_when_unknown():
    props = txt_properties(server_id=SERVER_ID, version="0.1.0", proto=1, name="x")
    assert "model" not in props


def test_txt_record_is_small_and_never_contains_a_key():
    props = txt_properties(server_id=SERVER_ID, version="0.1.0", proto=1, name="n" * 500, model="m" * 500)
    size = sum(len(k) + len(v.encode()) + 2 for k, v in props.items())
    assert size < 400
    assert "key" not in props


def test_get_local_ipv4s_valid_and_not_loopback():
    for ip in get_local_ipv4s():
        socket.inet_aton(ip)
        assert not ip.startswith("127.") and not ip.startswith("169.254.")


def test_get_local_ipv4s_filters(monkeypatch):
    adapters = [
        SimpleNamespace(ips=[SimpleNamespace(ip="127.0.0.1"), SimpleNamespace(ip="192.168.1.5")]),
        SimpleNamespace(ips=[SimpleNamespace(ip=("fe80::1", 0, 0)), SimpleNamespace(ip="169.254.1.1")]),
        SimpleNamespace(ips=[SimpleNamespace(ip="192.168.1.5"), SimpleNamespace(ip="10.0.0.2")]),
    ]
    import ifaddr

    monkeypatch.setattr(ifaddr, "get_adapters", lambda: adapters)
    assert get_local_ipv4s() == ["192.168.1.5", "10.0.0.2"]


class FakeAsyncZeroconf:
    def __init__(self, fail_register=False):
        self.registered = []
        self.unregistered = []
        self.closed = False
        self.fail_register = fail_register

    async def async_register_service(self, info):
        if self.fail_register:
            raise OSError("no multicast")
        self.registered.append(info)

    async def async_unregister_service(self, info):
        self.unregistered.append(info)

    async def async_close(self):
        self.closed = True


def advertiser(zc, **kw):
    return MdnsAdvertiser(
        DiscoveryConfig(name="Garage pump", model="F1245"),
        port=9477,
        server_id=SERVER_ID,
        version="0.1.0",
        addresses=["192.168.1.5", "10.0.0.2"],
        zeroconf_factory=lambda: zc,
        **kw,
    )


async def test_start_registers_service_and_stop_unregisters():
    zc = FakeAsyncZeroconf()
    adv = advertiser(zc)
    await adv.start()
    assert len(zc.registered) == 1
    info = zc.registered[0]
    assert info.type == SERVICE_TYPE
    assert info.name == "Garage pump-6f1c2a9e._nibe-usb._tcp.local."
    assert info.port == 9477
    assert sorted(info.addresses) == sorted(socket.inet_aton(a) for a in ("192.168.1.5", "10.0.0.2"))
    assert info.decoded_properties == {
        "id": SERVER_ID,
        "proto": "1",
        "ver": "0.1.0",
        "auth": "hmac-sha256",
        "name": "Garage pump",
        "model": "F1245",
    }
    await adv.stop()
    assert zc.unregistered == [info]
    assert zc.closed
    await adv.stop()  # idempotent


async def test_registration_failure_is_a_warning(caplog):
    zc = FakeAsyncZeroconf(fail_register=True)
    adv = advertiser(zc)
    with caplog.at_level(logging.WARNING):
        await adv.start()
    await adv.stop()
    assert any("mDNS registration failed" in r.getMessage() for r in caplog.records)
    assert zc.closed
    await adv.stop()  # nothing to do, must not raise


async def test_factory_failure_is_a_warning(caplog):
    def boom():
        raise OSError("no interfaces")

    adv = MdnsAdvertiser(
        DiscoveryConfig(),
        port=1,
        server_id=SERVER_ID,
        version="x",
        addresses=["10.0.0.2"],
        zeroconf_factory=boom,
    )
    with caplog.at_level(logging.WARNING):
        await adv.start()
    await adv.stop()
    assert any("mDNS registration failed" in r.getMessage() for r in caplog.records)


async def test_disabled_does_nothing():
    zc = FakeAsyncZeroconf()
    adv = MdnsAdvertiser(
        DiscoveryConfig(enabled=False),
        port=1,
        server_id=SERVER_ID,
        version="x",
        addresses=["10.0.0.2"],
        zeroconf_factory=lambda: zc,
    )
    await adv.start()
    assert zc.registered == []
    await adv.stop()


async def test_no_addresses_skips_registration(caplog, monkeypatch):
    monkeypatch.setattr(mdns, "get_local_ipv4s", lambda: [])
    zc = FakeAsyncZeroconf()
    adv = MdnsAdvertiser(
        DiscoveryConfig(), port=1, server_id=SERVER_ID, version="x", zeroconf_factory=lambda: zc
    )
    with caplog.at_level(logging.WARNING):
        await adv.start()
    assert zc.registered == []


def test_model_argument_overrides_config_default():
    adv = MdnsAdvertiser(
        DiscoveryConfig(model="cfg"), port=1, server_id=SERVER_ID, version="v", model="F1226"
    )
    assert adv.properties["model"] == "F1226"
    adv = MdnsAdvertiser(DiscoveryConfig(model="cfg"), port=1, server_id=SERVER_ID, version="v")
    assert adv.properties["model"] == "cfg"


async def test_address_change_triggers_reregistration(monkeypatch):
    zcs = []

    def factory():
        zc = FakeAsyncZeroconf()
        zcs.append(zc)
        return zc

    addresses = ["192.0.2.10"]
    monkeypatch.setattr("nibe_service_usb.discovery.mdns.get_local_ipv4s", lambda: list(addresses))
    adv = MdnsAdvertiser(
        DiscoveryConfig(name="Garage pump"),
        port=9477,
        server_id=SERVER_ID,
        version="0.1.0",
        zeroconf_factory=factory,
        check_interval=0.02,
    )
    await adv.start()
    try:
        assert len(zcs) == 1 and len(zcs[0].registered) == 1
        await asyncio.sleep(0.1)
        assert len(zcs) == 1  # unchanged addresses: nothing happens
        addresses[:] = ["192.0.2.20"]  # DHCP gave the Pi a new address
        for _ in range(100):
            if len(zcs) == 2 and zcs[1].registered:
                break
            await asyncio.sleep(0.02)
        assert zcs[0].unregistered and zcs[0].closed
        assert zcs[1].registered[0].addresses == [socket.inet_aton("192.0.2.20")]
    finally:
        await adv.stop()


async def test_registration_is_retried_after_failure(monkeypatch):
    zcs = []

    def factory():
        zc = FakeAsyncZeroconf(fail_register=not zcs)  # the first attempt fails
        zcs.append(zc)
        return zc

    monkeypatch.setattr("nibe_service_usb.discovery.mdns.get_local_ipv4s", lambda: ["192.0.2.20"])
    adv = MdnsAdvertiser(
        DiscoveryConfig(),
        port=9477,
        server_id=SERVER_ID,
        version="x",
        zeroconf_factory=factory,
        check_interval=0.02,
    )
    await adv.start()
    try:
        for _ in range(100):
            if len(zcs) == 2 and zcs[1].registered:
                break
            await asyncio.sleep(0.02)
        assert zcs[0].closed and zcs[1].registered
    finally:
        await adv.stop()
