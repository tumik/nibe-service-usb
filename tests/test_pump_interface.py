from __future__ import annotations

from datetime import datetime

import pytest

from nibe_service_usb.pump.interface import (
    WRITABLE_RAM,
    WRITABLE_REGISTERS,
    Clock,
    Priority,
    PumpError,
    PumpOffline,
    WriteFailed,
)


def make_clock(year: int, unixtime: int, host: float) -> Clock:
    return Clock(
        unixtime=unixtime,
        local=datetime(year, 1, 1, 12, 0, 0),
        dls=0,
        tz="Helsinki",
        w_offset=2,
        s_offset=3,
        host_unixtime=host,
    )


def test_clock_states() -> None:
    assert make_clock(2010, 1262375929, 1_760_000_000.0).state(120.0) == "unset"
    assert make_clock(2026, 1_760_000_050, 1_760_000_000.0).state(120.0) == "ok"
    assert make_clock(2026, 1_760_000_120, 1_760_000_000.0).state(120.0) == "ok"
    assert make_clock(2026, 1_760_000_500, 1_760_000_000.0).state(120.0) == "drifting"
    assert make_clock(2026, 1_759_999_000, 1_760_000_000.0).state(120.0) == "drifting"


def test_clock_drift() -> None:
    assert make_clock(2026, 1_760_000_010, 1_760_000_000.5).drift == 9.5


def test_priority_order() -> None:
    assert Priority.COMMAND < Priority.CONTROL < Priority.POLL


def test_errors() -> None:
    assert issubclass(PumpOffline, PumpError)
    err = WriteFailed("mismatch", readback=-5)
    assert err.readback == -5
    assert WriteFailed("x").readback is None


def test_allowlists() -> None:
    assert 2 in WRITABLE_REGISTERS[47041] and 3 not in WRITABLE_REGISTERS[47041]
    assert 1 in WRITABLE_REGISTERS[47050] and 2 not in WRITABLE_REGISTERS[47050]
    assert -15000 in WRITABLE_REGISTERS[40940] and 1 not in WRITABLE_REGISTERS[40940]
    assert WRITABLE_RAM == {0x2038AF18: 4}


def test_dm_ram_address_moves_the_single_ram_write_target() -> None:
    from nibe_service_usb.pump import codec
    from nibe_service_usb.pump.interface import DEFAULT_DM_RAM_ADDRESS, WRITABLE_RAM, set_dm_ram_address

    try:
        set_dm_ram_address(0x20390000)
        assert WRITABLE_RAM == {0x20390000: 4}
        codec.assert_allowed(codec.build_wm(0x20390000, (-20).to_bytes(4, "little", signed=True)))
        with pytest.raises(ValueError):
            codec.assert_allowed(codec.build_wm(DEFAULT_DM_RAM_ADDRESS, bytes(4)))
    finally:
        set_dm_ram_address(DEFAULT_DM_RAM_ADDRESS)
