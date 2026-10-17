"""Console command builders and reply parsers. Pure functions, no I/O.

Formats are byte-exact with the console's replies (see
``docs/pump-interface.md``). Builders return the command line without the
terminating CR LF; parsers accept a reply either with or without it (``bytes``
or ``str``), so the transport may strip the line terminator.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from .interface import WRITABLE_RAM, WRITABLE_REGISTERS, Clock, PumpProtocolError

VERB_ALLOWLIST: frozenset[str] = frozenset({"RV", "b", "QR", "AI", "RT", "RI", "RL", "RM", "WI", "WM"})

MAX_LINE_LENGTH = 61  # measured: 62 characters get no reply
MAX_RI_REGISTERS = 10  # measured: the parser silently drops an 11th

ReplyLike = bytes | bytearray | str

_RV_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*$")
_B_RE = re.compile(r"^\s*(\d+)\s*,\s*version:\s*([^,\s]+)\s*,\s*(.+?)\s*$")
_QR_RE = re.compile(r"^(\d{6})(\d{2})(\d{3})(\d{3})$")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_rv() -> str:
    return "RV"


def build_b() -> str:
    return "b"


def build_qr() -> str:
    return "QR"


def build_ai() -> str:
    return "AI"


def build_rt() -> str:
    return "RT 1"


def build_ri(
    registers: Sequence[int],
    *,
    max_line_length: int = MAX_LINE_LENGTH,
    max_registers: int = MAX_RI_REGISTERS,
) -> str:
    """``RI r1 r2 ...``; raises ``ValueError`` for >``max_registers`` registers or an over-long line."""
    if not registers:
        raise ValueError("RI needs at least one register")
    if len(registers) > max_registers:
        raise ValueError(f"RI takes at most {max_registers} registers, got {len(registers)}")
    for reg in registers:
        if not isinstance(reg, int) or isinstance(reg, bool) or reg < 0:
            raise ValueError(f"invalid register number {reg!r}")
    line = "RI " + " ".join(str(r) for r in registers)
    if len(line) > max_line_length:
        raise ValueError(f"RI line is {len(line)} characters, limit is {max_line_length}")
    return line


def build_rl(register: int) -> str:
    if register < 0:
        raise ValueError(f"invalid register number {register!r}")
    return f"RL {register}"


def build_rm(address: int, length: int) -> str:
    if address < 0 or address > 0xFFFFFFFF:
        raise ValueError(f"invalid address {address:#x}")
    if length < 1 or length > 32:
        raise ValueError(f"invalid RM length {length}")
    return f"RM 0x{address:08x} {length}"


def build_wi(register: int, value: int) -> str:
    return f"WI {register} {value}"


def build_wm(address: int, data: bytes) -> str:
    """``WM 0x<addr> <hex pairs>``; ``data`` is in memory order (little-endian for integers)."""
    if address < 0 or address > 0xFFFFFFFF:
        raise ValueError(f"invalid address {address:#x}")
    if not data:
        raise ValueError("WM needs at least one byte")
    return f"WM 0x{address:08x} {bytes(data).hex()}"


def assert_allowed(line: str, *, max_line_length: int = MAX_LINE_LENGTH) -> None:
    """Raise ``ValueError`` unless ``line`` may be sent to the port.

    Checks the verb allowlist, printable ASCII without CR/LF, the length limit
    and, for ``WI``/``WM``, the exact write allowlists of ``interface.py``.
    """
    if not line or any(ord(c) < 0x20 or ord(c) > 0x7E for c in line):
        raise ValueError(f"command contains forbidden characters: {line!r}")
    if len(line) > max_line_length:
        raise ValueError(f"command is {len(line)} characters, limit is {max_line_length}")
    parts = line.split()
    verb = parts[0]
    if verb not in VERB_ALLOWLIST:
        raise ValueError(f"verb {verb!r} is not allowed")
    if verb == "RI" and len(parts) - 1 > MAX_RI_REGISTERS:
        raise ValueError(f"RI takes at most {MAX_RI_REGISTERS} registers")
    if verb == "WI":
        if len(parts) != 3:
            raise ValueError(f"malformed WI: {line!r}")
        try:
            register, value = int(parts[1]), int(parts[2])
        except ValueError as exc:
            raise ValueError(f"malformed WI: {line!r}") from exc
        allowed = WRITABLE_REGISTERS.get(register)
        if allowed is None or value not in allowed:
            raise ValueError(f"WI {register} {value} is not on the write allowlist")
    elif verb == "WM":
        if len(parts) != 3:
            raise ValueError(f"malformed WM: {line!r}")
        try:
            address = int(parts[1], 16)
            data = bytes.fromhex(parts[2])
        except ValueError as exc:
            raise ValueError(f"malformed WM: {line!r}") from exc
        if WRITABLE_RAM.get(address) != len(data):
            raise ValueError(f"WM {parts[1]} ({len(data)} bytes) is not on the write allowlist")


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _text(reply: ReplyLike) -> str:
    if isinstance(reply, str):
        return reply
    return bytes(reply).decode("latin-1")


def parse_ri(reply: ReplyLike, expected_count: int) -> list[int]:
    tokens = _text(reply).split()
    if len(tokens) != expected_count:
        raise PumpProtocolError(f"RI reply has {len(tokens)} tokens, expected {expected_count}: {tokens!r}")
    try:
        return [int(t) for t in tokens]
    except ValueError as exc:
        raise PumpProtocolError(f"RI reply has a non-integer token: {tokens!r}") from exc


def parse_rt(reply: ReplyLike) -> list[int]:
    text = _text(reply).strip()
    if text.endswith(";"):
        text = text[:-1]
    fields = text.split(";")
    try:
        return [int(f) for f in fields]
    except ValueError as exc:
        raise PumpProtocolError(f"RT reply has a non-integer field: {text!r}") from exc


def parse_rm(reply: ReplyLike, expected_len: int) -> bytes:
    """Hex byte pairs, each followed by a NUL (``ec\\0ff\\0ff\\0ff\\0``), optionally ending in CR LF."""
    tokens = _text(reply).replace("\x00", " ").split()
    if len(tokens) != expected_len:
        raise PumpProtocolError(f"RM reply has {len(tokens)} bytes, expected {expected_len}: {tokens!r}")
    out = bytearray()
    for tok in tokens:
        if len(tok) != 2:
            raise PumpProtocolError(f"RM reply token is not a hex byte: {tok!r}")
        try:
            out.append(int(tok, 16))
        except ValueError as exc:
            raise PumpProtocolError(f"RM reply token is not a hex byte: {tok!r}") from exc
    return bytes(out)


def parse_ack(reply: ReplyLike) -> None:
    text = _text(reply).strip()
    if text != "1":
        raise PumpProtocolError(f"write was not acknowledged with '1': {text!r}")


def is_rv_reply(reply: ReplyLike) -> bool:
    return _RV_RE.match(_text(reply)) is not None


def parse_rv(reply: ReplyLike) -> tuple[int, int, int]:
    m = _RV_RE.match(_text(reply))
    if not m:
        raise PumpProtocolError(f"not an RV reply: {_text(reply)!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def parse_b(reply: ReplyLike) -> tuple[int, str, str]:
    """``1, version: 7994R7, F1226-6 R`` -> ``(brand, version, product)``."""
    m = _B_RE.match(_text(reply))
    if not m:
        raise PumpProtocolError(f"not a 'b' reply: {_text(reply)!r}")
    return int(m.group(1)), m.group(2), m.group(3)


def parse_qr(reply: ReplyLike) -> tuple[str, str, date | None, str]:
    """``AAAAAAYYDDDNNN`` -> ``(code, article, manufactured, serial)``; the date is ``None`` if invalid."""
    code = _text(reply).strip()
    m = _QR_RE.match(code)
    if not m:
        raise PumpProtocolError(f"not a 14 digit product code: {code!r}")
    article, yy, ddd, serial = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
    manufactured: date | None = None
    if 1 <= ddd <= 366:
        try:
            manufactured = date(2000 + yy, 1, 1) + timedelta(days=ddd - 1)
            if manufactured.year != 2000 + yy:
                manufactured = None
        except ValueError:
            manufactured = None
    return code, article, manufactured, serial


_AI_DATE = re.compile(r"Year\s+(\d+)\s+Month\s+(\d+)\s+Day\s+(\d+)")
_AI_TIME = re.compile(r"Hour\s+(\d+)\s+Minute\s+(\d+)\s+Second\s+(\d+)")
_AI_DLS = re.compile(r"DLS\s+(-?\d+)")
_AI_TZ = re.compile(r"TZ\s+(\S+)\s+W-Offset\s+(-?\d+)\s+S-Offset\s+(-?\d+)")
_AI_UNIX = re.compile(r"Unixtime\s+(\d+)")


def parse_ai(lines: Sequence[ReplyLike], host_unixtime: float) -> Clock:
    """The five ``AI`` lines -> ``Clock``. ``host_unixtime`` is the host clock at the time of the request."""
    if len(lines) != 5:
        raise PumpProtocolError(f"AI reply has {len(lines)} lines, expected 5")
    texts = [_text(line) for line in lines]
    try:
        m_date = _AI_DATE.search(texts[0])
        m_time = _AI_TIME.search(texts[1])
        m_dls = _AI_DLS.search(texts[2])
        m_tz = _AI_TZ.search(texts[3])
        m_unix = _AI_UNIX.search(texts[4])
        if not (m_date and m_time and m_dls and m_tz and m_unix):
            raise PumpProtocolError(f"unparsable AI reply: {texts!r}")
        local = datetime(
            int(m_date.group(1)),
            int(m_date.group(2)),
            int(m_date.group(3)),
            int(m_time.group(1)),
            int(m_time.group(2)),
            int(m_time.group(3)),
        )
    except ValueError as exc:
        raise PumpProtocolError(f"invalid date/time in AI reply: {texts!r}") from exc
    return Clock(
        unixtime=int(m_unix.group(1)),
        local=local,
        dls=int(m_dls.group(1)),
        tz=m_tz.group(1),
        w_offset=int(m_tz.group(2)),
        s_offset=int(m_tz.group(3)),
        host_unixtime=host_unixtime,
    )


# ---------------------------------------------------------------------------
# Integers in memory order
# ---------------------------------------------------------------------------


def encode_s32_le(value: int) -> bytes:
    """-256 -> ``00ffffff``, 256 -> ``00010000``, -20 -> ``ecffffff``."""
    return int(value).to_bytes(4, "little", signed=True)


def decode_s32_le(data: bytes) -> int:
    if len(data) != 4:
        raise ValueError(f"need 4 bytes, got {len(data)}")
    return int.from_bytes(data, "little", signed=True)
