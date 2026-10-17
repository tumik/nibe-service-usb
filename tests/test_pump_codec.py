from __future__ import annotations

from datetime import date, datetime

import pytest

from nibe_service_usb.pump import codec
from nibe_service_usb.pump.interface import PumpProtocolError

# -- builders -----------------------------------------------------------------------


def test_simple_builders() -> None:
    assert codec.build_rv() == "RV"
    assert codec.build_b() == "b"
    assert codec.build_qr() == "QR"
    assert codec.build_ai() == "AI"
    assert codec.build_rt() == "RT 1"
    assert codec.build_rl(47052) == "RL 47052"


def test_build_ri_matches_probe_log() -> None:
    assert codec.build_ri([40004]) == "RI 40004"
    assert codec.build_ri([40004, 40008]) == "RI 40004 40008"
    nine = [40004, 40008, 40012, 40014, 40013, 40015, 40016, 40017, 40018]
    line = codec.build_ri(nine)
    assert line == "RI 40004 40008 40012 40014 40013 40015 40016 40017 40018"
    assert len(line) == 56


def test_ri_register_count_limit() -> None:
    ten = list(range(1, 11))
    assert codec.build_ri(ten) == "RI 1 2 3 4 5 6 7 8 9 10"
    with pytest.raises(ValueError):
        codec.build_ri(list(range(1, 12)))
    with pytest.raises(ValueError):
        codec.build_ri([])


def test_ri_line_length_limit() -> None:
    # 10 five digit registers: 62 characters, the line that gets no reply on the console
    ten = [40004, 40008, 40012, 40014, 40013, 40015, 40016, 40017, 40018, 40019]
    with pytest.raises(ValueError):
        codec.build_ri(ten)
    # a 62 character line is only built when explicitly allowed
    assert len(codec.build_ri(ten, max_line_length=62)) == 62
    with pytest.raises(ValueError):
        codec.build_ri([40004] * 10, max_line_length=61)
    assert len(codec.build_ri([40004] * 9 + [1], max_line_length=61)) == 58
    with pytest.raises(ValueError):
        codec.build_ri([1, 2, 3], max_line_length=7)


def test_build_rm_wi_wm() -> None:
    assert codec.build_rm(0x2038AF18, 4) == "RM 0x2038af18 4"
    assert codec.build_rm(0x2066718C, 3) == "RM 0x2066718c 3"
    assert codec.build_wi(47050, 1) == "WI 47050 1"
    assert codec.build_wi(40940, -20) == "WI 40940 -20"
    assert codec.build_wm(0x2038AF18, codec.encode_s32_le(-20)) == "WM 0x2038af18 ecffffff"
    assert codec.build_wm(0x2038AF18, b"\x00\xff\xff\xff") == "WM 0x2038af18 00ffffff"
    with pytest.raises(ValueError):
        codec.build_wm(0x2038AF18, b"")


def test_every_builder_output_is_allowed() -> None:
    for line in (
        codec.build_rv(),
        codec.build_b(),
        codec.build_qr(),
        codec.build_ai(),
        codec.build_rt(),
        codec.build_ri([40004, 40008]),
        codec.build_rl(47052),
        codec.build_rm(0x2038AF18, 4),
        codec.build_wi(47041, 2),
        codec.build_wm(0x2038AF18, codec.encode_s32_le(-2000)),
    ):
        codec.assert_allowed(line)


# -- allowlist ---------------------------------------------------------------------


def test_verb_allowlist_contents() -> None:
    assert codec.VERB_ALLOWLIST == frozenset({"RV", "b", "QR", "AI", "RT", "RI", "RL", "RM", "WI", "WM"})


@pytest.mark.parametrize(
    "line",
    ["", "XX", "rv", "reset", "RI 1\r\nRV", "RV\n", "WI 40004 1", "WI 47041 3", "WI 47050 2", "WI 47041",
     "WM 0x20000000 00000000", "WM 0x2038af18 00", "WM 0x2038af18 0000000000", "WM 0x2038af18 zz",
     "RI " + " ".join(["1"] * 11), "RI 40004" + " " * 60],
)  # fmt: skip
def test_assert_allowed_rejects(line: str) -> None:
    with pytest.raises(ValueError):
        codec.assert_allowed(line)


def test_assert_allowed_write_allowlist_accepts() -> None:
    codec.assert_allowed("WI 47041 0")
    codec.assert_allowed("WI 47050 1")
    codec.assert_allowed("WI 40940 -15000")
    codec.assert_allowed("WM 0x2038af18 ecffffff")
    with pytest.raises(ValueError):
        codec.assert_allowed("WI 40940 1")
    with pytest.raises(ValueError):
        codec.assert_allowed("WI 40940 -15001")


def test_assert_allowed_line_length_boundary() -> None:
    line61 = "RI 40004" + " " * 47 + " 40008"
    assert len(line61) == 61
    codec.assert_allowed(line61)
    with pytest.raises(ValueError):
        codec.assert_allowed(line61 + " ")


# -- parsers, byte exact against console replies --------------------------------------


def test_parse_rv() -> None:
    assert codec.parse_rv(b"7994, 25, 0\r\n") == (7994, 25, 0)
    assert codec.parse_rv("7994, 25, 0") == (7994, 25, 0)
    assert codec.is_rv_reply(b"7994, 25, 0\r\n")
    for bad in (b"47 262 \r\n", b"1\r\n", b"7994, 25\r\n", b""):
        assert not codec.is_rv_reply(bad)
        with pytest.raises(PumpProtocolError):
            codec.parse_rv(bad)


def test_parse_b() -> None:
    assert codec.parse_b(b"1, version: 7994R7, F1226-6 R\r\n") == (1, "7994R7", "F1226-6 R")
    with pytest.raises(PumpProtocolError):
        codec.parse_b(b"7994, 25, 0\r\n")


def test_parse_qr() -> None:
    code, article, manufactured, serial = codec.parse_qr(b"06522820001001\r\n")
    assert (code, article, serial) == ("06522820001001", "065228", "001")
    assert manufactured == date(2020, 1, 1)
    assert codec.parse_qr(b"06522820000001\r\n")[2] is None  # day 0
    assert codec.parse_qr(b"06522817999002\r\n")[2] is None  # day 999
    for bad in (b"0652282000100\r\n", b"abcdefghijklmn\r\n", b"7994, 25, 0\r\n"):
        with pytest.raises(PumpProtocolError):
            codec.parse_qr(bad)


AI_LINES = [
    b"Year 2010 Month 1 Day 1",
    b"Hour 22 Minute 58 Second 49",
    b"DLS 0",
    b"TZ Helsinki W-Offset 2 S-Offset 3",
    b"Unixtime 1262375929",
]


def test_parse_ai() -> None:
    clock = codec.parse_ai(AI_LINES, 1_760_000_000.0)
    assert clock.unixtime == 1262375929
    assert clock.local == datetime(2010, 1, 1, 22, 58, 49)
    assert (clock.dls, clock.tz, clock.w_offset, clock.s_offset) == (0, "Helsinki", 2, 3)
    assert clock.host_unixtime == 1_760_000_000.0
    assert clock.state(120.0) == "unset"
    with_crlf = [line + b"\r\n" for line in AI_LINES]
    assert codec.parse_ai(with_crlf, 1.0) == codec.parse_ai(AI_LINES, 1.0)


def test_parse_ai_errors() -> None:
    with pytest.raises(PumpProtocolError):
        codec.parse_ai(AI_LINES[:4], 0.0)
    with pytest.raises(PumpProtocolError):
        codec.parse_ai([*AI_LINES[:4], b"Unixtime x"], 0.0)
    with pytest.raises(PumpProtocolError):
        codec.parse_ai([b"Year 2010 Month 13 Day 1", *AI_LINES[1:]], 0.0)


RT_LINE = (
    b"47;254;226;0;0;485;530;0;0;202;169;234;226;315;236;315;269;"
    b"-32768;-32768;-32768;-32768;0;0;0;-32768;0;0;0;\r\n"
)


def test_parse_rt() -> None:
    values = codec.parse_rt(RT_LINE)
    assert len(values) == 28
    assert values[:3] == [47, 254, 226]
    assert values[17] == -32768
    assert values[-1] == 0
    assert codec.parse_rt(RT_LINE.rstrip()) == values
    with pytest.raises(PumpProtocolError):
        codec.parse_rt(b"1;2;x;\r\n")


def test_parse_ri() -> None:
    assert codec.parse_ri(b"46 \r\n", 1) == [46]
    assert codec.parse_ri(b"46 254 \r\n", 2) == [46, 254]
    assert codec.parse_ri(b"-20 96854 \r\n", 2) == [-20, 96854]
    assert codec.parse_ri(b"47 262 227 480 529 208 174 237 312 236 \r\n", 10)[-1] == 236
    assert codec.parse_ri("0 ", 1) == [0]


def test_parse_ri_token_count_mismatch() -> None:
    stale = b"47 262 227 480 529 208 174 237 312 236 \r\n"
    with pytest.raises(PumpProtocolError):
        codec.parse_ri(stale, 9)
    with pytest.raises(PumpProtocolError):
        codec.parse_ri(b"46 \r\n", 2)
    with pytest.raises(PumpProtocolError):
        codec.parse_ri(b"\r\n", 1)


def test_parse_ri_not_int() -> None:
    with pytest.raises(PumpProtocolError):
        codec.parse_ri(b"7994, 25, 0\r\n", 3)
    with pytest.raises(PumpProtocolError):
        codec.parse_ri(b"abc 0 \r\n", 2)
    with pytest.raises(PumpProtocolError):  # RI 1 prints the product code string
        codec.parse_ri(b"06522820001001 0 0 0 0 0 0 0 0  \r\n", 10)


def test_parse_rm_with_nul_separators() -> None:
    assert codec.parse_rm(b"ec\x00ff\x00ff\x00ff\x00\r\n", 4) == bytes.fromhex("ecffffff")
    assert codec.parse_rm(b"ec\x00ff\x00ff\x00ff\x00", 4) == bytes.fromhex("ecffffff")
    assert codec.parse_rm(b"00\x0000\x0000\x00\r\n", 3) == b"\x00\x00\x00"
    assert codec.decode_s32_le(codec.parse_rm(b"00\x00ff\x00ff\x00ff\x00\r\n", 4)) == -256


def test_parse_rm_errors() -> None:
    with pytest.raises(PumpProtocolError):
        codec.parse_rm(b"ec\x00ff\x00\r\n", 4)
    with pytest.raises(PumpProtocolError):
        codec.parse_rm(b"zz\x00ff\x00ff\x00ff\x00\r\n", 4)
    with pytest.raises(PumpProtocolError):
        codec.parse_rm(b"1\r\n", 4)


def test_parse_ack() -> None:
    assert codec.parse_ack(b"1\r\n") is None
    assert codec.parse_ack("1") is None
    for bad in (b"0\r\n", b"\r\n", b"46 254 \r\n", b"11\r\n"):
        with pytest.raises(PumpProtocolError):
            codec.parse_ack(bad)


# -- little endian ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "hexstr"),
    [(-256, "00ffffff"), (256, "00010000"), (-20, "ecffffff"), (0, "00000000"), (-2000, "30f8ffff")],
)
def test_s32_le(value: int, hexstr: str) -> None:
    assert codec.encode_s32_le(value).hex() == hexstr
    assert codec.decode_s32_le(bytes.fromhex(hexstr)) == value


def test_decode_s32_wrong_length() -> None:
    with pytest.raises(ValueError):
        codec.decode_s32_le(b"\x00\x00\x00")


# -- console samples (command, exact reply bytes) ---------------------------------------

SAMPLES: list[tuple[bytes, bytes]] = [
    (b"RI 40004\r\n", b"46 \r\n"),
    (b"RI 40004 40008\r\n", b"46 254 \r\n"),
    (b"RI 47041 43109 40313 47206 47375 43110\r\n", b"1 1 0 -200 180 0 \r\n"),
    (b"RI 43071 40969 40965 40967\r\n", b"12345 1000 0 0 \r\n"),
    (b"RI 99999\r\n", b"0 \r\n"),
    (b"RI 40004 40008 40012 40014 40013 40015 40016 40017 40018 40019\r\n", b""),  # 62 chars
    (b"RV\r\n", b"46 254 227 480 529 208 174 237 312 236 \r\n"),  # the shifted reply
    (b"RM 0x2038af18 4\r\n", b"ec\x00ff\x00ff\x00ff\x00\r\n"),
    (b"RM 0x2066718C 3\r\n", b"00\x0000\x0000\x00\r\n"),
    (b"WM 0x2038af18 00ffffff\r\n", b"1\r\n"),
    (b"WM 0x2038af18 00010000\r\n", b"1\r\n"),
    (b"WI 40940 -20\r\n", b"1\r\n"),
]


def test_sample_commands_are_reproducible() -> None:
    """Every RI/RM/WI/WM sample command is what our builders produce, and replies parse."""
    checked = 0
    for cmd, reply in SAMPLES:
        text = cmd.decode().strip()
        verb, *args = text.split()
        if verb == "RI" and reply and len(text) <= 61 and len(args) <= 10:
            assert codec.build_ri([int(a) for a in args]) == text
            codec.parse_ri(reply, len(args))
            checked += 1
        elif verb == "RM":
            address, length = int(args[0], 16), int(args[1])
            assert codec.build_rm(address, length).lower() == text.lower()
            assert len(codec.parse_rm(reply, length)) == length
            checked += 1
        elif verb == "WM":
            data = bytes.fromhex(args[1])
            assert codec.build_wm(int(args[0], 16), data).lower() == text.lower()
            codec.parse_ack(reply)
            checked += 1
        elif verb == "WI":
            assert codec.build_wi(int(args[0]), int(args[1])) == text
            codec.parse_ack(reply)
            checked += 1
    assert checked == 10
    # the shifted 10-value reply that arrives instead of an RV answer must be rejected
    with pytest.raises(PumpProtocolError):
        codec.parse_rv(SAMPLES[6][1])
    assert codec.decode_s32_le(codec.parse_rm(SAMPLES[7][1], 4)) == -20


def test_over_long_line_gets_no_reply_and_is_refused() -> None:
    """The 62 character line gets no reply on the console; builders refuse to produce it."""
    for cmd, reply in SAMPLES:
        text = cmd.decode().strip()
        if len(text) > 61:
            assert reply == b""
            with pytest.raises(ValueError):
                codec.assert_allowed(text)
