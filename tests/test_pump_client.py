"""``SerialPumpClient`` against a fake transport that models the real console quirks."""

from __future__ import annotations

import asyncio
import dataclasses
from collections import deque
from collections.abc import AsyncIterator

import pytest

from nibe_service_usb.config import PumpConfig
from nibe_service_usb.pump import codec
from nibe_service_usb.pump.client import SerialPumpClient
from nibe_service_usb.pump.interface import (
    Priority,
    PumpOffline,
    PumpProtocolError,
    PumpTimeout,
    WriteFailed,
)
from nibe_service_usb.pump.transport import TransportError

DM_ADDR = 0x2038AF18
PI_ADDR = 0x2066718C

REGISTERS = {
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
    47050: 1,
    40940: 0,
    40969: 96859,
}


class FakeTransport:
    """In-memory console. Mirrors the measured behaviour, including the shifted reply:

    a command flagged with ``lose_next`` gets no reply; its reply is delivered in place of the
    next command's reply and that next command is swallowed.
    """

    def __init__(self) -> None:
        self.registers = dict(REGISTERS)
        self.sent: list[str] = []
        self.opened = 0
        self.closed = 0
        self.down = False  # port gone: open() fails
        self.silent = False  # port open but the pump does not answer
        self.lose_next = False  # next command loses its reply (shifted-reply failure)
        self.drop_next = False  # next command is dropped without a stale reply
        self.stale: list[bytes] | None = None
        self.bad_reply: dict[str, list[bytes]] = {}
        self.ignore_writes = False
        self.rt_fields = 28
        self.gate: asyncio.Event | None = None
        self.active = 0
        self.max_active = 0
        self.drained = 0
        self.raise_transport_error = False

    async def open(self) -> None:
        if self.down:
            raise TransportError("no such device")
        self.opened += 1

    async def close(self) -> None:
        self.closed += 1

    async def drain_input(self) -> None:
        self.drained += 1

    async def request(self, line: str, *, expect_lines: int = 1, timeout: float) -> list[bytes]:  # noqa: ASYNC109
        codec.assert_allowed(line)
        self.sent.append(line)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                gate, self.gate = self.gate, None
                await gate.wait()
            if self.raise_transport_error:
                raise TransportError("port vanished")
            if self.silent or self.down:
                raise PumpTimeout(f"no reply to {line!r}")
            if self.stale is not None:
                stale, self.stale = self.stale, None
                return stale  # the shifted reply; this command itself is swallowed
            if self.drop_next:
                self.drop_next = False
                raise PumpTimeout(f"no reply to {line!r}")
            reply = self.bad_reply.get(line.split()[0]) or self._reply(line)
            if self.lose_next:
                self.lose_next = False
                self.stale = reply
                raise PumpTimeout(f"no reply to {line!r}")
            assert len(reply) == expect_lines
            return reply
        finally:
            self.active -= 1

    def _reply(self, line: str) -> list[bytes]:
        verb, *args = line.split()
        if verb == "RV":
            return [b"7994, 25, 0"]
        if verb == "b":
            return [b"1, version: 7994R7, F1226-6 R"]
        if verb == "QR":
            return [b"06522820001001"]
        if verb == "AI":
            return [
                b"Year 2010 Month 1 Day 1",
                b"Hour 22 Minute 58 Second 49",
                b"DLS 0",
                b"TZ Helsinki W-Offset 2 S-Offset 3",
                b"Unixtime 1262375929",
            ]
        if verb == "RT":
            return [(";".join(str(i) for i in range(self.rt_fields)) + ";").encode()]
        if verb == "RI":
            return [("".join(f"{self.registers.get(int(a), 0)} " for a in args)).encode()]
        if verb == "RM":
            address, length = int(args[0], 16), int(args[1])
            if address == DM_ADDR:
                data = codec.encode_s32_le(self.registers[40940])
            else:
                data = bytes(length)
            return [b"".join(f"{b:02x}".encode() + b"\x00" for b in data)]
        if verb == "WI":
            if not self.ignore_writes:
                self.registers[int(args[0])] = int(args[1])
            return [b"1"]
        if verb == "WM":
            if not self.ignore_writes:
                self.registers[40940] = codec.decode_s32_le(bytes.fromhex(args[1]))
            return [b"1"]
        raise AssertionError(line)


CFG = PumpConfig(port="fake", request_timeout=0.2, retries=2, offline_after=60.0)


async def make_client(
    fake: FakeTransport | None = None, config: PumpConfig = CFG, **kwargs: float
) -> tuple[SerialPumpClient, FakeTransport]:
    fake = fake or FakeTransport()
    client = SerialPumpClient(
        config,
        transport_factory=lambda _cfg: fake,
        reconnect_interval=kwargs.pop("reconnect_interval", 0.01),
        reconnect_max=kwargs.pop("reconnect_max", 0.04),
    )
    await client.start()
    return client, fake


@pytest.fixture
async def started() -> AsyncIterator[tuple[SerialPumpClient, FakeTransport]]:
    client, fake = await make_client()
    yield client, fake
    await client.stop()


async def wait_for(predicate, timeout: float = 2.0) -> None:  # noqa: ANN001, ASYNC109
    async with asyncio.timeout(timeout):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.005)


# -- basics ------------------------------------------------------------------------------


async def test_start_verifies_pump_with_b(started) -> None:  # noqa: ANN001
    client, fake = started
    assert client.online
    assert client.port == "fake"
    assert fake.sent == ["b"]
    assert client.last_ok is not None
    assert client.error_count == 0


async def test_read_single(started) -> None:  # noqa: ANN001
    client, fake = started
    assert await client.read(40004) == 47
    assert fake.sent[-1] == "RI 40004"


async def test_read_many_chunks_and_never_exceeds_line_limit(started) -> None:  # noqa: ANN001
    client, fake = started
    regs = [40004 + i for i in range(40)]
    for r in regs:
        fake.registers[r] = r - 40000
    values = await client.read_many(regs)
    assert values == [r - 40000 for r in regs]
    ri_lines = [line for line in fake.sent if line.startswith("RI")]
    assert [len(line.split()) - 1 for line in ri_lines] == [9, 9, 9, 9, 4]
    assert max(len(line) for line in fake.sent) <= 61
    assert await client.read_many([]) == []


async def test_read_many_respects_line_length_even_with_larger_batch_setting() -> None:
    cfg = dataclasses.replace(CFG, ri_batch_size=10)
    client, fake = await make_client(config=cfg)
    regs = list(range(40000, 40023))  # five digit registers: 10 would need 62 characters
    await client.read_many(regs)
    ri_lines = [line for line in fake.sent if line.startswith("RI")]
    assert all(len(line) <= 61 for line in ri_lines)
    assert [len(line.split()) - 1 for line in ri_lines] == [9, 9, 5]
    # short register numbers do fit 10 per line
    fake.sent.clear()
    await client.read_many(list(range(1, 24)))
    assert [len(line.split()) - 1 for line in fake.sent] == [10, 10, 3]
    await client.stop()


async def test_read_temperatures(started) -> None:  # noqa: ANN001
    client, fake = started
    values = await client.read_temperatures()
    assert values == list(range(28))
    assert fake.sent[-1] == "RT 1"


async def test_read_temperatures_wrong_field_count_is_an_error() -> None:
    client, fake = await make_client()
    fake.rt_fields = 27
    with pytest.raises(PumpProtocolError):
        await client.read_temperatures()
    assert client.error_count >= 3
    assert client.online  # a pump that answers RV is not offline
    await client.stop()


async def test_read_ram(started) -> None:  # noqa: ANN001
    client, fake = started
    fake.registers[40940] = -20
    assert await client.read_ram(DM_ADDR, 4) == bytes.fromhex("ecffffff")
    assert fake.sent[-1] == "RM 0x2038af18 4"
    assert await client.read_ram(PI_ADDR, 3) == b"\x00\x00\x00"


async def test_identity(started) -> None:  # noqa: ANN001
    client, fake = started
    ident = await client.identity()
    assert fake.sent[-3:] == ["b", "RV", "QR"]
    assert (ident.product, ident.software_version, ident.brand) == ("F1226-6 R", "7994R7", 1)
    assert (ident.software_id, ident.base_version) == (7994, 25)
    assert (ident.product_code, ident.article, ident.serial) == ("06522820001001", "065228", "001")
    assert ident.manufactured is not None and ident.manufactured.isoformat() == "2020-01-01"


async def test_clock(started) -> None:  # noqa: ANN001
    client, fake = started
    clock = await client.clock()
    assert fake.sent[-1] == "AI"
    assert clock.unixtime == 1262375929
    assert clock.state(120.0) == "unset"
    assert clock.host_unixtime > 1_700_000_000


# -- writes ---------------------------------------------------------------------------------


async def test_write_sends_wi_and_reads_back(started) -> None:  # noqa: ANN001
    client, fake = started
    await client.write(47041, 2)
    assert fake.sent[-2:] == ["WI 47041 2", "RI 47041"]
    assert fake.registers[47041] == 2


async def test_write_dm_register_is_allowed(started) -> None:  # noqa: ANN001
    client, fake = started
    await client.write(40940, -20)
    assert fake.registers[40940] == -20


@pytest.mark.parametrize(
    ("register", "value"), [(40004, 1), (47041, 3), (47050, 2), (40940, 1), (40940, -15001)]
)
async def test_write_outside_allowlist_sends_nothing(started, register: int, value: int) -> None:  # noqa: ANN001
    client, fake = started
    before = list(fake.sent)
    with pytest.raises(ValueError):
        await client.write(register, value)
    assert fake.sent == before


async def test_write_readback_mismatch_raises_write_failed(started) -> None:  # noqa: ANN001
    client, fake = started
    fake.ignore_writes = True
    with pytest.raises(WriteFailed) as err:
        await client.write(47041, 2)
    assert err.value.readback == 1


async def test_write_rejected_ack_raises_write_failed_after_one_resync_retry() -> None:
    client, fake = await make_client()
    fake.bad_reply["WI"] = [b"0"]
    with pytest.raises(WriteFailed):
        await client.write(47041, 2)
    assert fake.sent[1:] == ["WI 47041 2", "RV", "WI 47041 2", "RV"]
    await client.stop()


async def test_write_without_reply_resyncs_and_retries_once(started) -> None:  # noqa: ANN001
    client, fake = started
    fake.drop_next = True
    await client.write(47050, 0)
    assert fake.sent[1:] == ["WI 47050 0", "RV", "WI 47050 0", "RI 47050"]
    assert fake.registers[47050] == 0


async def test_write_ram_sends_wm_and_reads_back_with_rm(started) -> None:  # noqa: ANN001
    client, fake = started
    await client.write_ram(DM_ADDR, codec.encode_s32_le(-1999))
    assert fake.sent[-2:] == ["WM 0x2038af18 31f8ffff", "RM 0x2038af18 4"]
    assert fake.registers[40940] == -1999
    assert fake.registers[40969] == 96859  # WM costs no EEPROM store in the fake either


async def test_write_ram_rejects_unlisted_target(started) -> None:  # noqa: ANN001
    client, fake = started
    before = list(fake.sent)
    with pytest.raises(ValueError):
        await client.write_ram(0x20000000, bytes(4))
    with pytest.raises(ValueError):
        await client.write_ram(DM_ADDR, bytes(3))
    with pytest.raises(ValueError):
        await client.write_ram(DM_ADDR, codec.encode_s32_le(5))  # positive DM is outside the range
    assert fake.sent == before


async def test_write_ram_readback_mismatch(started) -> None:  # noqa: ANN001
    client, fake = started
    fake.ignore_writes = True
    fake.registers[40940] = 7
    with pytest.raises(WriteFailed) as err:
        await client.write_ram(DM_ADDR, codec.encode_s32_le(-20))
    assert err.value.readback == 7


# -- shifted reply and resync ------------------------------------------------------------------


async def test_shifted_reply_is_discarded_and_caller_gets_correct_data(started) -> None:  # noqa: ANN001
    """Over-long line: no reply; the next command gets the stale 10-token reply and is swallowed."""
    client, fake = started
    regs = [40004, 40008, 40012, 40014, 40013, 40015, 40016, 40017, 40018]
    fake.registers.update({r: i + 100 for i, r in enumerate(regs)})
    fake.lose_next = True
    values = await client.read_many(regs)
    assert values == [100 + i for i in range(9)]
    ri = "RI " + " ".join(map(str, regs))
    # original (no reply) -> RV gets the stale RI reply (rejected) -> RV valid -> retry of the original
    assert fake.sent[1:] == [ri, "RV", "RV", ri]
    assert fake.drained >= 1
    assert client.error_count >= 2
    assert client.online


async def test_stale_ten_token_reply_never_accepted_as_data(started) -> None:  # noqa: ANN001
    client, fake = started
    fake.stale = [b"47 262 227 480 529 208 174 237 312 236 "]
    # The very next command is swallowed and answered with the stale 10-token line.
    assert await client.read(40004) == 47
    assert fake.sent[1:] == ["RI 40004", "RV", "RI 40004"]


async def test_timeout_then_successful_resync_keeps_pump_online(started) -> None:  # noqa: ANN001
    client, fake = started
    fake.drop_next = True
    assert await client.read(40008) == 262
    assert fake.sent[1:] == ["RI 40008", "RV", "RI 40008"]
    assert client.online


async def test_silent_pump_raises_timeout_but_stays_online_before_offline_after(started) -> None:  # noqa: ANN001
    client, fake = started
    fake.silent = True
    with pytest.raises(PumpTimeout):
        await client.read(40004)
    assert client.online
    assert fake.sent.count("RV") == 3  # max three RV tries
    assert client.error_count >= 4
    fake.silent = False
    assert await client.read(40004) == 47


# -- offline / reconnect --------------------------------------------------------------------------


async def test_offline_then_reconnect_then_online() -> None:
    cfg = dataclasses.replace(CFG, offline_after=0.0)
    client, fake = await make_client(config=cfg)
    events: list[bool] = []
    done = asyncio.Event()

    async def async_cb(online: bool) -> None:
        events.append(online)
        if online:
            done.set()

    sync_events: list[bool] = []
    client.on_state_change(async_cb)
    client.on_state_change(sync_events.append)

    fake.silent = True
    fake.down = True  # port vanished: reopening fails until it is back
    with pytest.raises(PumpOffline):
        await client.read(40004)
    assert not client.online
    assert events == [False] == sync_events

    # requests fail immediately and are not queued or sent
    sent = len(fake.sent)
    with pytest.raises(PumpOffline):
        await client.read(40004)
    with pytest.raises(PumpOffline):
        await client.write(47041, 1)
    assert len(fake.sent) == sent

    # port comes back after a few failed reopen attempts
    await asyncio.sleep(0.1)
    assert fake.opened == 1  # only the initial open so far
    fake.down = False
    fake.silent = False
    await asyncio.wait_for(done.wait(), 2.0)
    assert client.online
    assert events == [False, True] == sync_events
    assert fake.opened >= 2 and fake.closed >= 2  # closed and reopened
    assert fake.sent[-1] == "b"  # online is declared after a successful b reply
    assert await client.read(40004) == 47
    await client.stop()


async def test_reconnect_requires_valid_b_reply() -> None:
    cfg = dataclasses.replace(CFG, offline_after=0.0)
    client, fake = await make_client(config=cfg)
    fake.silent = True
    with pytest.raises(PumpOffline):
        await client.read(40004)
    fake.silent = False
    fake.bad_reply["b"] = [b"7994, 25, 0"]
    await asyncio.sleep(0.1)
    assert not client.online
    attempts = fake.sent.count("b")
    assert attempts >= 2  # keeps trying
    fake.bad_reply.clear()
    await wait_for(lambda: client.online)
    await client.stop()


async def test_transport_error_goes_offline_immediately() -> None:
    client, fake = await make_client()
    fake.raise_transport_error = True
    with pytest.raises(PumpOffline):
        await client.read(40004)
    assert not client.online
    fake.raise_transport_error = False
    await wait_for(lambda: client.online)
    await client.stop()


async def test_start_with_unreachable_pump_then_connects() -> None:
    fake = FakeTransport()
    fake.down = True
    client = SerialPumpClient(
        CFG, transport_factory=lambda _c: fake, reconnect_interval=0.01, reconnect_max=0.02
    )
    seen: list[bool] = []
    client.on_state_change(seen.append)
    await client.start()  # does not raise
    assert not client.online
    with pytest.raises(PumpOffline):
        await client.read(40004)
    fake.down = False
    await wait_for(lambda: client.online)
    assert seen == [True]
    assert await client.read(40004) == 47
    await client.stop()


async def test_failing_callback_does_not_break_the_client() -> None:
    cfg = dataclasses.replace(CFG, offline_after=0.0)
    client, fake = await make_client(config=cfg)

    def boom(_online: bool) -> None:
        raise RuntimeError("callback bug")

    client.on_state_change(boom)
    fake.silent = True
    with pytest.raises(PumpOffline):
        await client.read(40004)
    fake.silent = False
    await wait_for(lambda: client.online)
    await client.stop()


# -- queue ------------------------------------------------------------------------------------------


async def test_priority_order_and_single_request_in_flight(started) -> None:  # noqa: ANN001
    client, fake = started
    gate = asyncio.Event()
    fake.gate = gate
    first = asyncio.create_task(client.read(1001, priority=Priority.POLL))
    await wait_for(lambda: fake.active == 1)
    poll = asyncio.create_task(client.read(1002, priority=Priority.POLL))
    control = asyncio.create_task(client.read(1003, priority=Priority.CONTROL))
    command = asyncio.create_task(client.read(1004, priority=Priority.COMMAND))
    poll2 = asyncio.create_task(client.read(1005, priority=Priority.POLL))
    await asyncio.sleep(0.02)
    gate.set()
    await asyncio.gather(first, poll, control, command, poll2)
    assert fake.sent[1:] == ["RI 1001", "RI 1004", "RI 1003", "RI 1002", "RI 1005"]
    assert fake.max_active == 1


async def test_cancelled_caller_does_not_block_the_queue(started) -> None:  # noqa: ANN001
    client, fake = started
    gate = asyncio.Event()
    fake.gate = gate
    first = asyncio.create_task(client.read(1001))
    await wait_for(lambda: fake.active == 1)
    cancelled = asyncio.create_task(client.read(1002))
    await asyncio.sleep(0.01)
    cancelled.cancel()
    gate.set()
    await first
    assert await client.read(1003) == 0
    assert "RI 1002" not in fake.sent


async def test_stop_fails_pending_requests_with_pump_offline() -> None:
    client, fake = await make_client()
    gate = asyncio.Event()
    fake.gate = gate
    in_flight = asyncio.create_task(client.read(1001))
    await wait_for(lambda: fake.active == 1)
    queued = asyncio.create_task(client.read(1002))
    await asyncio.sleep(0.01)
    await client.stop()
    for task in (in_flight, queued):
        with pytest.raises(PumpOffline):
            await task
    assert not client.online
    assert fake.closed >= 1
    with pytest.raises(PumpOffline):
        await client.read(1003)


# -- the real transport against a pty ------------------------------------------------------------------


async def test_serial_transport_rejects_bad_lines_before_touching_the_port() -> None:
    from nibe_service_usb.pump.transport import SerialTransport

    transport = SerialTransport(CFG)
    for bad in ("reset", "WI 40004 1", "RI 40004" + " " * 60, "RV\r\nRV"):
        with pytest.raises(ValueError):
            await transport.request(bad, timeout=0.1)
    with pytest.raises(TransportError):  # allowed line, but the port is not open
        await transport.request("RV", timeout=0.1)


async def test_serial_transport_roundtrip_over_pty() -> None:
    import os
    import pty
    import tty

    from nibe_service_usb.pump.transport import SerialTransport

    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    loop = asyncio.get_running_loop()
    received: list[bytes] = []
    replies = deque(
        [
            b"ec\x00ff\x00ff\x00ff\x00\r\n",  # RM reply with NUL bytes
            b"Year 2010 Month 1 Day 1\r\nHour 22 Minute 58 Second 49\r\nDLS 0\r\n"
            b"TZ Helsinki W-Offset 2 S-Offset 3\r\nUnixtime 1262375929\r\n",
            b"46 254 \r\n",
        ]
    )

    def on_readable() -> None:
        data = os.read(master, 1024)
        received.append(data)
        if data.endswith(b"\r\n") and replies:
            os.write(master, replies.popleft())

    loop.add_reader(master, on_readable)
    transport = SerialTransport(dataclasses.replace(CFG, port=os.ttyname(slave)))
    try:
        await transport.open()
        assert await transport.request("RM 0x2038af18 4", timeout=1.0) == [b"ec\x00ff\x00ff\x00ff\x00"]
        lines = await transport.request("AI", expect_lines=5, timeout=1.0)
        assert len(lines) == 5 and lines[4] == b"Unixtime 1262375929"
        os.write(master, b"stale garbage\r\n")
        await transport.drain_input()
        assert await transport.request("RI 40004 40008", timeout=1.0) == [b"46 254 "]
        assert received == [b"RM 0x2038af18 4\r\n", b"AI\r\n", b"RI 40004 40008\r\n"]
        with pytest.raises(PumpTimeout):
            await transport.request("RV", timeout=0.1)  # no scripted reply left
    finally:
        loop.remove_reader(master)
        await transport.close()
        os.close(master)
        os.close(slave)
