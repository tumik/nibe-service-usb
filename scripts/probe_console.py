#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyserial>=3.5"]
# ///
"""Probe of the Nibe service USB console (F1226-6 R).

Sends commands one at a time, waits for each CR LF terminated reply and prints
the raw bytes plus an interpretation. The default run is READ-ONLY (RV, b, QR, AI,
RT 1, RI). Writing modes are explicit: --write-test adds one degree-minute write
(WI 40940 -20, then reads it back with the EEPROM store counter), and --eeprom and
--memwrite are write experiments. Only write values you know are safe for your unit.

Modes:
    uv run scripts/probe_console.py                  # default read set (read-only)
    uv run scripts/probe_console.py --write-test     # plus one degree-minute write test (WI 40940 -20)
    uv run scripts/probe_console.py --limits         # find the RI line limit (reads only)
    uv run scripts/probe_console.py --eeprom         # EEPROM counter: background rate, then 3 DM writes
    uv run scripts/probe_console.py --memwrite       # WM RAM writes to the DM address + PI timer
    uv run scripts/probe_console.py --dry-run        # print what would be sent, open nothing
    uv run scripts/probe_console.py --only RV AI     # subset by label
    uv run scripts/probe_console.py --log probe.log  # append a transcript

Console behaviour this script is built around:
  * every reply ends with CR LF; RI values are space separated with a trailing
    space ("46 254 \\r\\n"); WI answers "1\\r\\n"; unknown id answers "0 \\r\\n";
  * a 62-character RI line (10 registers) gets NO reply; its reply comes
    back as the answer to the NEXT command, and that next command is lost.
    Hence --limits, the per-reply token-count check and the RV sync commands.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import time
from dataclasses import dataclass

# name, scale, unit for the RI interpretations
REGS: dict[int, tuple[str, float, str]] = {
    40004: ("BT1 outdoor", 0.1, "°C"),
    40008: ("BT2 flow", 0.1, "°C"),
    40012: ("BT3 return", 0.1, "°C"),
    40014: ("BT6 hot water charge", 0.1, "°C"),
    40013: ("BT7 hot water top", 0.1, "°C"),
    40015: ("BT10 brine in", 0.1, "°C"),
    40016: ("BT11 brine out", 0.1, "°C"),
    40017: ("BT12 condenser out", 0.1, "°C"),
    40018: ("BT14 hot gas", 0.1, "°C"),
    40019: ("BT15 liquid line", 0.1, "°C"),
    40022: ("BT17 suction gas", 0.1, "°C"),
    40067: ("avg outdoor", 0.1, "°C"),
    43009: ("calculated flow", 0.1, "°C"),
    41788: ("control flow", 0.1, "°C"),
    40940: ("degree minutes (raw = 10 x DM)", 0.1, "DM"),
    47041: ("hot water comfort setting (0 eco,1 normal,2 lux)", 1, ""),
    43109: ("hot water mode (-1 off,0 eco,1 normal,2 lux,3 periodic)", 1, ""),
    40313: ("hot water reason code", 1, ""),
    47206: ("start compressor setting (DM, x10 = raw threshold)", 1, "DM"),
    47375: ("stop heating setting", 0.1, "°C"),
    47137: ("op. mode (0 auto,1 manual,2 add heat only)", 1, ""),
    43110: ("heat demand (0/1)", 1, ""),
    43427: ("compressor FSM (20 idle,40 start,60 run,100 stop)", 1, ""),
    43435: ("compressor relay command", 1, ""),
    43443: ("PCA base compressor status (5 stop,6 run)", 1, ""),
    43086: ("op. priority (10 off,20 hw,30 heat)", 1, ""),
    43405: ("compressor start inhibit", 1, "min"),
    43437: ("GP1 speed", 1, "%"),
    43439: ("GP2 speed", 1, "%"),
    43516: ("AA1 heater relay bits", 1, ""),
    43084: ("electrical addition power", 0.01, "kW"),
    43012: ("any general alarm (0/1)", 1, ""),
    43151: ("AUX block heating", 1, ""),
    43112: ("compressor permission mask", 1, ""),
    32041: ("PCA base version", 1, ""),
    45119: ("alarm log next slot", 1, ""),
    43416: ("compressor starts", 1, ""),
    43420: ("compressor hours total", 1, "h"),
    43424: ("compressor hours hot water", 1, "h"),
    43071: ("uptime, seconds since boot", 1, "s"),
    40969: ("EEPROM store-function call count", 1, ""),
    40965: ("EEPROM write failures, total", 1, ""),
    40967: ("EEPROM write failures since boot", 1, ""),
    99999: ("UNKNOWN id - expect '0 '", 1, ""),
}

SENTINELS = {-32768: "sensor open/short (0x8000)", -32767: "over range (0x8001)"}
CRLF = b"\r\n"


@dataclass(frozen=True)
class Cmd:
    label: str
    cmd: str
    parser: str  # rv | b | qr | ai | rt | ri | wi | sync
    pre_delay: float = 0.0  # seconds to wait before sending (e.g. let the pump integrate)
    note: str = ""


def ri(label: str, *regs: int, **kw) -> Cmd:
    return Cmd(label, "RI " + " ".join(str(r) for r in regs), "ri", **kw)


def sync(label: str) -> Cmd:
    return Cmd(label, "RV", "sync", note="sync: expects '7994, 25, 0'; anything else is a stale reply")


DEFAULT: list[Cmd] = [
    Cmd("RV", "RV", "rv"),
    Cmd("b", "b", "b"),
    Cmd("QR", "QR", "qr"),
    Cmd("AI", "AI", "ai"),
    Cmd("RT1", "RT 1", "rt"),
    ri("RI-single", 40004),
    ri("RI-two", 40004, 40008),
    ri("RI-temps-a", 40012, 40014, 40013, 40015, 40016, 40017),
    ri("RI-temps-b", 40018, 40019, 40022, 40067, 43009, 41788),
    ri("RI-heating", 40940, 47206, 47375, 43110, 47137, 43151),
    ri("RI-hotwater", 47041, 43109, 40313, 43112),
    ri("RI-compressor-a", 43427, 43435, 43443, 43086, 43405),
    ri("RI-compressor-b", 43437, 43439, 43516, 43084, 43012),
    ri("RI-identity", 32041, 45119, 43416, 43420, 43424),
    ri("RI-health", 43071, 40969, 40965, 40967),
    ri("RI-unknown", 99999),
]

WRITE_TEST: list[Cmd] = [
    ri("WI-before", 40940, 40969),
    Cmd("WI-dm", "WI 40940 -20", "wi", note="DM raw -20 = -2.0 DM; only if this value is safe for your unit"),
    ri("WI-after", 40940, 40969),
    ri(
        "WI-after-70s",
        40940,
        43110,
        40969,
        pre_delay=70,
        note="after one pump integration step: does the pump keep, integrate or reset our -20?",
    ),
]

# Line-limit probe. Each candidate is followed by an RV sync so a lost command or
# a shifted reply is visible immediately. All reads; unknown ids 1..11 return 0.
_T = [40004, 40008, 40012, 40014, 40013, 40015, 40016, 40017, 40018, 40019, 40022]
LIMITS: list[Cmd] = [
    sync("sync-0"),
    ri("LIM-6regs-38ch", *_T[:6]),
    sync("sync-6"),
    ri("LIM-7regs-44ch", *_T[:7]),
    sync("sync-7"),
    ri("LIM-8regs-50ch", *_T[:8]),
    sync("sync-8"),
    ri("LIM-9regs-56ch", *_T[:9]),
    sync("sync-9"),
    Cmd("LIM-10regs-23ch", "RI 1 2 3 4 5 6 7 8 9 10", "ri", note="10 registers, short line"),
    sync("sync-10s"),
    Cmd("LIM-11regs-26ch", "RI 1 2 3 4 5 6 7 8 9 10 11", "ri", note="11 registers, short line"),
    sync("sync-11s"),
    Cmd("LIM-2regs-62ch", "RI 40004" + " " * 48 + " 40008", "ri", note="2 registers padded to 62 chars"),
    sync("sync-2l"),
    Cmd("LIM-2regs-58ch", "RI 40004" + " " * 44 + " 40008", "ri", note="2 registers padded to 58 chars"),
    sync("sync-2m"),
    Cmd("LIM-2regs-59ch", "RI 40004" + " " * 45 + " 40008", "ri", note="2 registers padded to 59 chars"),
    sync("sync-59"),
    Cmd("LIM-2regs-60ch", "RI 40004" + " " * 46 + " 40008", "ri", note="2 registers padded to 60 chars"),
    sync("sync-60"),
    Cmd("LIM-2regs-61ch", "RI 40004" + " " * 47 + " 40008", "ri", note="2 registers padded to 61 chars"),
    sync("sync-61"),
    ri("LIM-10regs-62ch", *_T[:10], note="62 characters: expected to get no reply"),
    sync("sync-10l"),
    sync("sync-end"),
]


# EEPROM test: does R40969 grow in the background, and does each DM write add one?
EEPROM: list[Cmd] = [
    ri("EE-t0", 40969, 43071, 40940, 43110),
    ri("EE-t30", 40969, 43071, pre_delay=30),
    ri("EE-t60", 40969, 43071, pre_delay=30),
    ri("EE-t120", 40969, 43071, pre_delay=60),
    ri("EE-t180", 40969, 43071, pre_delay=60),
    ri("EE-before-writes", 40940, 40969),
    Cmd("EE-w1", "WI 40940 -20", "wi", note="only if this value is safe for your unit"),
    Cmd("EE-w2", "WI 40940 -21", "wi", note="-2.1 DM"),
    Cmd("EE-w3", "WI 40940 -22", "wi", note="-2.2 DM"),
    ri("EE-after-writes", 40940, 40969),
    ri("EE-after-60s", 40940, 40969, 43110, pre_delay=60),
    ri("EE-after-120s", 40940, 40969, 43110, pre_delay=60),
]


DM_RAM = "0x2038af18"  # RAM copy of R40940 (s32 LE), writable with WM without an EEPROM store
PI_TIMER = "0x2066718C"  # periodic increase run timer: h, m, s remaining


def le32(v: int) -> str:
    """Little-endian hex of a signed 32-bit value: -256 -> '00ffffff', 256 -> '00010000'."""
    return v.to_bytes(4, "little", signed=True).hex()


# Memory-write test: WM to the DM RAM address must change R40940 without
# touching the EEPROM counter. Example values are +-256 (+-25.6 DM).
MEMWRITE: list[Cmd] = [
    ri("MW-t0", 40940, 40969, 43110),
    Cmd("MW-rm-dm", f"RM {DM_RAM} 4", "rm", note="read the 4 DM bytes (expect hex pairs, NUL separated)"),
    Cmd("MW-w-20", f"WM {DM_RAM} {le32(-20)}", "wm", note="raw -20 = -2.0 DM"),
    ri("MW-after-20", 40940, 40969),
    Cmd("MW-rm-after-20", f"RM {DM_RAM} 4", "rm"),
    Cmd("MW-w-256", f"WM {DM_RAM} 00ffffff", "wm", note="example: raw -256 = -25.6 DM"),
    ri("MW-after-256", 40940, 40969),
    Cmd("MW-w+256", f"WM {DM_RAM} 00010000", "wm", note="example: raw +256 = +25.6 DM"),
    ri("MW-after+256", 40940, 40969),
    Cmd("MW-pi-timer", f"RM {PI_TIMER} 3", "rm", note="periodic increase countdown h m s (read only)"),
    ri("MW-pi-flags", 43014, 47050, 43109, 40313, note="hold flag, periodic enable bit, hw mode, hw reason"),
    ri("MW-after-60s", 40940, 40969, 43110, pre_delay=60),
]


# ----------------------------------------------------------------------------
# Parsers: text -> human readable lines. Never raise.
# ----------------------------------------------------------------------------


def parse_rv(text: str) -> list[str]:
    parts = [p.strip() for p in text.split(",")]
    out = [f"fields: {parts}"]
    if len(parts) >= 2:
        out.append(f"software id = {parts[0]}, PCA base version = {parts[1]}")
    return out


def parse_sync(text: str) -> list[str]:
    if re.fullmatch(r"\s*\d+,\s*\d+,\s*\d+\s*", text):
        return ["sync OK (RV reply)"]
    return [
        f"!! SYNC FAILED: got {text.strip()!r} instead of an RV reply -> "
        "previous command's reply arrived late or this one was swallowed"
    ]


def parse_b(text: str) -> list[str]:
    m = re.search(r"(\d+)\s*,\s*version:\s*(\S+)\s*,\s*(.+)", text)
    if not m:
        return ["could not parse (expected '1, version: 7994R7, F1226-6 R')"]
    return [f"brand byte = {m.group(1)}, software version = {m.group(2)}, product = {m.group(3).strip()}"]


def parse_qr(text: str) -> list[str]:
    code = text.strip()
    out = [f"product code = {code!r} ({len(code)} chars)"]
    if len(code) == 14 and code.isdigit():
        article, yyddd, rest = code[:6], code[6:11], code[11:]
        try:
            d = dt.datetime.strptime(yyddd, "%y%j").date()
            out.append(f"article = {article}, date = {d} (YYDDD {yyddd}), serial = {rest}")
        except ValueError:
            out.append(f"article = {article}, date field = {yyddd} (unparsable), serial = {rest}")
    return out


def parse_ai(text: str) -> list[str]:
    out = []
    m = re.search(r"Unixtime\s+(\d+)", text)
    if m:
        pump = int(m.group(1))
        now = time.time()
        out.append(
            f"pump unixtime = {pump} = {dt.datetime.fromtimestamp(pump, dt.timezone.utc):%Y-%m-%d %H:%M:%S} UTC"
        )
        out.append(f"host  unixtime = {now:.0f}; drift pump-host = {pump - now:+.0f} s")
        if pump < 1_600_000_000:
            out.append(
                "!! clock looks UNSET (counts from 2010-01-01 since boot?) - compare with uptime R43071"
            )
    m = re.search(r"Year\s+(\d+)\s+Month\s+(\d+)\s+Day\s+(\d+)", text)
    m2 = re.search(r"Hour\s+(\d+)\s+Minute\s+(\d+)\s+Second\s+(\d+)", text)
    if m and m2:
        out.append(
            f"pump local time = {m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d} "
            f"{int(m2.group(1)):02d}:{int(m2.group(2)):02d}:{int(m2.group(3)):02d}"
        )
        out.append(f"host local time = {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    m = re.search(r"DLS\s+(\d+)", text)
    if m:
        out.append(f"DLS = {m.group(1)}")
    m = re.search(r"TZ\s+(\S+)\s+W-Offset\s+(-?\d+)\s+S-Offset\s+(-?\d+)", text)
    if m:
        out.append(f"TZ = {m.group(1)}, winter offset = {m.group(2)}, summer offset = {m.group(3)}")
    return out or ["could not parse"]


def fmt_temp(raw: int) -> str:
    return SENTINELS.get(raw, f"{raw / 10:.1f} °C")


def parse_rt(text: str) -> list[str]:
    fields = [f.strip() for f in text.strip().rstrip(";").split(";")]
    out = [
        f"{len(fields)} ';'-separated fields (field N = BT N)"
    ]
    for i, f in enumerate(fields, start=1):
        try:
            raw = int(f)
            out.append(f"  field {i:2d} (BT{i:<2d}) raw {raw:7d} -> {fmt_temp(raw)}")
        except ValueError:
            out.append(f"  field {i:2d} raw {f!r} (not an int)")
    return out


def parse_ri(text: str, cmd: str) -> list[str]:
    regs = [int(r) for r in cmd.split()[1:]]
    tokens = text.split()
    out = [f"{len(tokens)} token(s) for {len(regs)} register(s): {tokens}"]
    if len(tokens) != len(regs):
        out.append(
            "!! TOKEN COUNT MISMATCH: this is probably the reply of an EARLIER command (line-limit overflow)"
        )
    for i, reg in enumerate(regs):
        name, scale, unit = REGS.get(reg, (f"reg {reg}", 1, ""))
        if i >= len(tokens):
            out.append(f"  R{reg} {name}: <no token>")
            continue
        try:
            raw = int(tokens[i])
        except ValueError:
            out.append(f"  R{reg} {name}: raw {tokens[i]!r} (not an int)")
            continue
        if unit == "°C" and raw in SENTINELS:
            val = SENTINELS[raw]
        elif reg == 43071:
            val = f"{raw} s = {raw // 86400} d {(raw % 86400) // 3600:02d}:{(raw % 3600) // 60:02d}:{raw % 60:02d}"
        elif scale == 1:
            val = f"{raw}{(' ' + unit) if unit else ''}"
        else:
            val = f"{raw * scale:g} {unit}".strip()
        out.append(f"  R{reg} {name}: raw {raw} -> {val}")
    return out


def parse_rm(text: str) -> list[str]:
    cleaned = text.replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    toks = cleaned.split()
    out = [f"{len(toks)} token(s) after removing NULs: {toks}"]
    try:
        data = bytes(int(t, 16) for t in toks)
    except ValueError:
        return out + ["not all tokens are hex bytes"]
    out.append(f"bytes = {data.hex(' ')}")
    if len(data) == 4:
        out.append(
            f"as s32 little-endian = {int.from_bytes(data, 'little', signed=True)}  (big-endian would be {int.from_bytes(data, 'big', signed=True)})"
        )
    if len(data) == 3:
        h, m, sec = data
        out.append(
            f"as h/m/s = {h:02d}:{m:02d}:{sec:02d} remaining -> periodic increase {'ACTIVE' if (h, m, sec) != (0, 0, 0) else 'not running'}"
        )
    return out


def parse_wm(text: str) -> list[str]:
    tok = text.strip()
    return [f"WM reply {tok!r} ({'same ack as WI' if tok == '1' else 'format not known yet'})"]


def parse_wi(text: str) -> list[str]:
    tok = text.strip()
    return (
        ["write acknowledged ('1')"] if tok == "1" else [f"!! unexpected write reply {tok!r} (expected '1')"]
    )


PARSERS = {
    "rv": parse_rv,
    "sync": parse_sync,
    "b": parse_b,
    "qr": parse_qr,
    "ai": parse_ai,
    "rt": parse_rt,
    "wi": parse_wi,
    "rm": parse_rm,
    "wm": parse_wm,
}


def interpret(c: Cmd, text: str) -> list[str]:
    try:
        if c.parser == "ri":
            return parse_ri(text, c.cmd)
        return PARSERS[c.parser](text)
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return [f"parser error: {exc!r}"]


# ----------------------------------------------------------------------------
# Serial I/O
# ----------------------------------------------------------------------------


def read_reply(ser, idle: float, max_wait: float) -> tuple[bytes, float | None]:
    """Read until the line is silent for `idle` s (after at least one CR LF) or
    `max_wait` s total. Returns (bytes, seconds to first byte or None)."""
    buf = bytearray()
    start = time.monotonic()
    last = start
    first: float | None = None
    while True:
        chunk = ser.read(256)
        now = time.monotonic()
        if chunk:
            if first is None:
                first = now - start
            buf += chunk
            last = now
        elif buf and buf.endswith(CRLF) and now - last >= idle:
            break
        elif buf and now - last >= max(idle, 1.0):
            break
        if now - start >= max_wait:
            break
    return bytes(buf), first


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200, help="CDC-ACM ignores it; kept for completeness")
    ap.add_argument("--idle", type=float, default=0.3, help="seconds of silence after CR LF that end a reply")
    ap.add_argument("--max-wait", type=float, default=5.0, help="max seconds to wait for one reply")
    ap.add_argument("--delay", type=float, default=0.5, help="pause between commands")
    ap.add_argument("--no-rts", action="store_true", help="do not assert RTS (doc says it must be set)")
    ap.add_argument("--no-dtr", action="store_true", help="do not assert DTR")
    ap.add_argument("--only", nargs="*", metavar="LABEL", help="run only these labels")
    ap.add_argument("--log", metavar="PATH", help="append a raw transcript to this file")
    ap.add_argument(
        "--dry-run", action="store_true", help="print the commands and exit without opening the port"
    )
    ap.add_argument(
        "--write-test",
        action="store_true",
        help="also run the degree-minute write test (WI 40940 -20, writes to the pump)",
    )
    ap.add_argument(
        "--limits",
        action="store_true",
        help="run the RI line-limit probe instead of the default set (reads only)",
    )
    ap.add_argument(
        "--eeprom",
        action="store_true",
        help="run the EEPROM counter test (3 min of reads, then 3 DM writes -20/-21/-22)",
    )
    ap.add_argument(
        "--memwrite",
        action="store_true",
        help="run the WM (RAM write) test on the DM address and read the periodic-increase timer",
    )
    args = ap.parse_args()

    if args.limits:
        cmds = list(LIMITS)
    elif args.eeprom:
        cmds = list(EEPROM)
    elif args.memwrite:
        cmds = list(MEMWRITE)
    else:
        cmds = list(DEFAULT) + (list(WRITE_TEST) if args.write_test else [])
    if args.only:
        wanted = set(args.only)
        cmds = [c for c in cmds if c.label in wanted]
        missing = wanted - {c.label for c in cmds}
        if missing:
            print(f"unknown labels: {sorted(missing)}", file=sys.stderr)
            return 2

    print(
        f"port={args.port} baud={args.baud} rts={not args.no_rts} dtr={not args.no_dtr} "
        f"idle={args.idle}s max_wait={args.max_wait}s delay={args.delay}s mode={'limits' if args.limits else 'eeprom' if args.eeprom else 'memwrite' if args.memwrite else 'default'}"
    )
    print("commands that will be sent (each followed by CR LF):")
    for c in cmds:
        extra = f"  [wait {c.pre_delay:.0f}s first]" if c.pre_delay else ""
        note = f"  # {c.note}" if c.note else ""
        print(f"  [{c.label:16s}] {len(c.cmd):2d} ch {c.cmd!r}{extra}{note}")
    if args.dry_run:
        print("dry run: nothing sent")
        return 0

    import serial  # pyserial, imported late so --dry-run works without it

    log = open(args.log, "a", encoding="utf-8") if args.log else None

    def logln(s: str) -> None:
        if log:
            log.write(s + "\n")
            log.flush()

    observed: dict[str, list[str]] = {}
    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = args.baud
    ser.timeout = 0.1
    ser.rts = not args.no_rts
    ser.dtr = not args.no_dtr
    ser.open()
    logln(
        f"# {dt.datetime.now().isoformat()} opened {args.port} mode={'limits' if args.limits else 'eeprom' if args.eeprom else 'memwrite' if args.memwrite else 'default'}"
    )
    try:
        ser.reset_input_buffer()
        pending, _ = read_reply(ser, idle=0.5, max_wait=1.5)
        if pending:
            print(f"\n[unsolicited data before first command] {pending!r}")
            logln(f"< (unsolicited) {pending!r}")
        else:
            print("\nno unsolicited data in the first 1.5 s")

        for c in cmds:
            if c.pre_delay:
                print(f"\n... waiting {c.pre_delay:.0f} s before [{c.label}]")
                time.sleep(c.pre_delay)
            wire = c.cmd.encode("ascii") + CRLF
            print(f"\n=== [{c.label}] send {wire!r}" + (f"   # {c.note}" if c.note else ""))
            logln(f"> {wire!r}")
            t0 = time.monotonic()
            ser.write(wire)
            ser.flush()
            raw, first = read_reply(ser, args.idle, args.max_wait)
            elapsed = time.monotonic() - t0
            fb = f"first byte {first:.3f}s" if first is not None else "no bytes"
            logln(f"< {raw!r}  ({fb}, total {elapsed:.2f}s)")
            print(f"    raw ({len(raw)} bytes, {fb}, total {elapsed:.2f} s): {raw!r}")
            if not raw:
                print(
                    "    !! NO REPLY within max-wait (if this is an RI line: probably over the line limit; expect its reply on the next command)"
                )
            else:
                text = raw.decode("latin-1")
                for line in text.replace("\r", "").split("\n"):
                    if line:
                        print(f"    | {line}")
                for line in interpret(c, text):
                    print(f"    = {line}")
                observed[c.label] = text.split()
            time.sleep(args.delay)

        if "WI-before" in observed and "WI-after" in observed:
            b, a = observed["WI-before"], observed["WI-after"]
            print("\n=== write test summary")
            print(f"    DM raw before {b[0] if b else '?'} -> after {a[0] if a else '?'} (expected -20)")
            if len(b) > 1 and len(a) > 1:
                try:
                    d = int(a[1]) - int(b[1])
                    print(
                        f"    EEPROM store calls {b[1]} -> {a[1]} (delta {d}): "
                        f"{'a DM write DOES cost an EEPROM store call' if d > 0 else 'no EEPROM store seen for the DM write'}"
                    )
                except ValueError:
                    pass
            l = observed.get("WI-after-70s")
            if l and len(l) >= 2:
                print(
                    f"    DM raw 70 s later {l[0]} with heat demand {l[1]}: "
                    f"{'pump RESET it (heat demand 0 -> DM forced to 0)' if l[0] == '0' else 'pump kept/integrated it'}"
                )
        if "MW-t0" in observed and "MW-after+256" in observed:
            print("\n=== memory write test summary (R40940, R40969)")
            for lab in ("MW-t0", "MW-after-20", "MW-after-256", "MW-after+256", "MW-after-60s"):
                v = observed.get(lab)
                if v:
                    print(f"    {lab:14s} {' '.join(v)}")
            try:
                d = int(observed["MW-after+256"][1]) - int(observed["MW-t0"][1])
                vals = [observed["MW-after-20"][0], observed["MW-after-256"][0], observed["MW-after+256"][0]]
                print(f"    read-backs after WM -20 / -256 / +256: {vals} (expected ['-20', '-256', '256'])")
                print(
                    f"    EEPROM store calls delta across 3 WM writes: {d} ({'NO EEPROM cost' if d == 0 else 'EEPROM touched!'})"
                )
            except (KeyError, ValueError, IndexError):
                pass
        if "EE-t0" in observed and "EE-after-writes" in observed:
            print("\n=== EEPROM test summary (R40969 store calls, R43071 uptime)")
            for lab in (
                "EE-t0",
                "EE-t30",
                "EE-t60",
                "EE-t120",
                "EE-t180",
                "EE-before-writes",
                "EE-after-writes",
                "EE-after-60s",
                "EE-after-120s",
            ):
                v = observed.get(lab)
                if v:
                    print(f"    {lab:18s} {' '.join(v)}")
            try:
                bg = int(observed["EE-t180"][0]) - int(observed["EE-t0"][0])
                wr = int(observed["EE-after-writes"][1]) - int(observed["EE-before-writes"][1])
                print(f"    background increase over ~180 s without writes: {bg}")
                print(
                    f"    increase across 3 DM writes: {wr} ({'one call per write' if wr == 3 else 'NOT one per write'})"
                )
            except (KeyError, ValueError, IndexError):
                pass
    finally:
        ser.close()
        logln(f"# {dt.datetime.now().isoformat()} closed")
        if log:
            log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
