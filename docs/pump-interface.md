# Pump interface (service USB console)

This document describes what the server uses, as tested on an F1226-6 R
with display firmware 7994R7. **Register numbers and RAM
addresses may differ on other models and firmware versions**; run the
read-only probes and compare before trusting the server with another unit.

## Physical / serial layer

| Item | Value |
|---|---|
| Device | USB type B service port, CDC-ACM (`03eb:6119`), appears as `/dev/ttyACM0` |
| Config key | `pump.port`, default `/dev/ttyACM0`; prefer `/dev/serial/by-id/…` if several ACM devices exist |
| Baud rate | `pump.baudrate`, default 115200. **Does not matter**: CDC-ACM is a virtual serial port over USB, the value is never used as a line speed. It is configurable only so the driver can be told something sensible. |
| Control lines | **RTS must be asserted**; the server asserts RTS and DTR on open. Configurable `pump.rts` / `pump.dtr`, default true. |
| Line ending | Commands end with **CR LF** (`\r\n`). Replies are text; line structure per command below. |
| Echo | **None.** The device does not echo the command; what comes back is the reply only. |
| Discipline | One command at a time; wait for the reply (or timeout) before the next. Never two lines in one write. One client only on the port. |
| Reply framing | Every reply ends with **CR LF**; `AI` is 5 CR LF lines. Values in `RI` replies are space separated with a trailing space (`46 254 \r\n`). The transport reads up to CR LF (5 lines for `AI`) with `pump.request_timeout` (default 3 s). Measured first-byte latency is well under 0.3 s. |
| **Line limit (measured)** | Command text up to **61 characters** works, **62 fails** (so a 64-byte buffer holding text + CR LF + NUL). A too-long line gets **no reply**; its reply is then delivered as the answer to the **next** command, and that next command is swallowed. Independently, `RI` accepts at most **10 registers** (an 11th is dropped silently). With 5-digit register numbers 9 registers fit in 56 characters, so **`pump.ri_batch_size = 9`** and a hard check `len(line) <= 61` in the transport. |
| Reply validation | Every `RI` reply must contain exactly as many tokens as registers requested; `WI` must answer `1`; `RV` must match `\d+, \d+, \d+`. A mismatch marks the transport **desynchronised**: discard, send `RV` until a valid `RV` reply arrives (max 3 tries), then retry the original request once. Never accept a mismatched reply as data. |

Everything the console returns is the **display's RAM copy** of a value, not a
live query to the PCA Base board. Values update at the 1 s control cycle,
degree minutes once per minute.

## Commands the server uses

Only these. The transport layer has a hard allowlist of command verbs (`RV b
QR AI RT RI RL RM WI WM`); anything else raises before reaching the port.

| Command | Reply (example) | Use |
|---|---|---|
| `RV` | `7994, 25, 0` | software id, **PCA base version** (25), third field unknown |
| `b` | `1, version: 7994R7, F1226-6 R` | brand byte, **software version** `7994R7`, **product** `F1226-6 R` |
| `QR` | `06522820001001` (example) | 14-char product code: article `065228`, date `20001` = YYDDD (2020 day 001), serial `001` |
| `AI` | 5 lines: `Year 2010 Month 1 Day 1` / `Hour 22 Minute 21 Second 45` / `DLS 0` / `TZ Helsinki W-Offset 2 S-Offset 3` / `Unixtime 1262373705` | clock check: compare `Unixtime` to host time, report drift |
| `RT 1` | `v1;v2;…;v28` raw s16 ×0.1 °C | all BT temperatures in one line; field N is BT N (confirmed by the probe, see below; unused slots are constant 0 or -32768) |
| `RI r1 [r2 … r9]` | raw integer(s), space separated, trailing space, CR LF: `46 254 ` | any register by number; **9 per line** (line ≤ 61 chars; the parser caps at 10 registers). Prints `0 ` for unknown ids in the 3xxxx–4xxxx range and for time/date typed registers; very low ids print other things (`RI 1` printed the product code string, `RI 10` printed an empty token), so only the documented register ids are ever sent. |
| `RL r` | seconds / day number | time-of-day and date typed registers (`47052`, `44259`, `47526`). Allowed by the transport but not used yet. |
| `RM 0x<addr> <n>` | `n` hex byte pairs, **each followed by a NUL byte**, then CR LF (`ec\0ff\0ff\0ff\0\r\n`) | raw RAM read. Used for the DM RAM copy (`RM 0x2038af18 4`, s32 little-endian) and the periodic-increase run timer (`RM 0x2066718C 3`, h m s). The parser strips NULs before decoding. |
| `WI r v` | `1` | register write; goes through the firmware's "is this register persisted?" check and **costs one EEPROM store call** for persisted registers (measured for R40940). Used for settings that must persist (R47041, R47050), and for R40940 only with `dm_write_method = "register"`. |
| `WM 0x<addr> <hex>` | `1` | raw RAM write, bytes as hex pairs in memory order (little-endian for integers): `WM 0x2038af18 00ffffff` = raw -256, `00010000` = +256. **No EEPROM store** (measured: counter unchanged across three writes, read-back via `RI 40940` correct). This is how degree minutes are written. |

Sentinels for temperatures: `-32768` = sensor open/short (shown `--` on the
display), `-32767` = over range. Published to HA as `null`.

## Write commands

Two verbs, both answer `1` CR LF on success:

* `WI <register> <value>` — register write with persistence. Measured: one
  EEPROM store call per write on R40940, so it is used **only for settings
  that should persist** (hot water comfort, periodic increase enable bit).
* `WM 0x<address> <hexbytes>` — raw RAM write, little-endian byte order for
  integers, no EEPROM store. Used for degree minutes at the RAM copy of
  R40940, address `0x2038af18`, 4 bytes (`WM 0x2038af18 ecffffff` = -20).
  Read back with `RI 40940` (same value) or `RM 0x2038af18 4`.

Every write is handled as: send, wait up to `pump.request_timeout` for the
`1` CR LF reply, then read the register back. No reply → one `RV` resync,
one retry of the write, then `write_failed`. A reply other than `1`, or a
read-back that differs from the value written → `write_failed` with the
read-back value in the error. `write_failed` is reported to HA and logged at
`WARNING`; the controllers do not retry on their own until their next
scheduled evaluation.

The transport allowlist is exact (`pump/interface.py`): `WI` only for
registers in `WRITABLE_REGISTERS = {47041: range(0, 3), 47050: range(0, 2),
40940: range(-15000, 1)}` (R40940 only for the `register` fallback) and `WM`
only for `WRITABLE_RAM = {0x2038af18: 4}` with the decoded value inside
`-15000..0`. The heating controller additionally clamps to
`[dm_min, dm_max]`. Any other register, address or value raises before
reaching the port. The API layer cannot name a register or address at all.

| Operation | Verb | Target | Value | Read-back |
|---|---|---|---|---|
| Hot water comfort mode | `WI` | R47041 (s8) | 0 eco, 1 normal, 2 lux | R47041, and R43109 for the effective mode |
| Degree minutes | `WM` | `0x2038af18` (s32 LE, = R40940) | `dm_disable_value` (-1) or `R47206 × 10 + 1` | R40940 |
| Periodic increase now | `WI` ×2 | R47050 (enable bit) | `0`, wait 5 s, `1` | run timer `RM 0x2066718C 3` ≠ 0 and R43109 = 3 within ~15 s |

`dm_write_method = "register"` in config switches degree minutes back to
`WI 40940` (EEPROM-costly) in case a firmware update moves the RAM address;
the address itself is `heating.dm_ram_address` and is validated at startup by
checking that `RM` of it decodes to the same value `RI 40940` returns.

### Periodic hot water increase (anti-legionella) — trigger and status

Trigger sequence (`trigger_hot_water_boost`), all at `COMMAND` priority:

1. Read R47050. Remember it. Read the run timer; if non-zero → error
   `already_running`.
2. `WI 47050 0`, verify `1`.
3. Wait 5 s (other queued requests may run in between; the 5 s is a minimum).
4. `WI 47050 1`, verify `1`. The periodic increase is now **enabled** in the
   pump's settings even if it was 0 before; the result reports
   `enabled_periodic_increase: true` in that case so HA can show it.
5. Poll the run timer every 3 s for up to 15 s. Timer ≠ 0 (or R43109 = 3)
   → success, `data = {started: true, remaining_seconds: …}`. Otherwise
   error `trigger_failed` with the flags read (R43109, R40313, R43014).

Cost: two EEPROM store calls per trigger; rate limited to one per 10 minutes.

Status (core poll, plus right after a trigger):

| key | source | meaning |
|---|---|---|
| `pi_running` | `RM 0x2066718C 3` ≠ `00 00 00` | increase in progress |
| `pi_remaining` | same, `h×3600 + m×60 + s` | seconds the run can still last (counts down from 5 h, ends earlier once target is held for 1 h) |
| `pi_hold` | `RI 43014` = 1 | target temperature reached, 1 h hold phase (also 1 when idle; only meaningful while `pi_running`) |
| `pi_enabled` | `RI 47050` | periodic increase enable bit |
| `pi_failure` | `RI 45111` | last run failed to reach the target |

These are core sensors and are read in every fast poll (every 60 s), so HA
sees the progress; HA can combine `pi_running`, `pi_hold` and `pi_remaining`
into a single state.

## Startup / health check sequence

Run (a) at every (re)connect of the serial port, (b) when the pump has not
answered for `pump.offline_after` (default 60 s) and then answers again, and
(c) once per day at `pump.daily_check_time` (default 03:30 local):

1. `b` → product name, software version. A change compared with the value
   seen earlier in this run → `WARNING` (firmware update or different unit).
2. `RV` → PCA base version; cross-check with `RI 32041` (`WARNING` on mismatch).
3. `QR` → product code; a change is logged the same way (sanity that we talk
   to the same pump).
4. `AI` → clock drift vs host; `WARNING` above `pump.max_clock_drift`
   (default 120 s). Exposed as `get_status.identity.clock_state` and
   `clock_drift`.
5. `RI 40969 40965 40967` → EEPROM health:
   * R40969 = number of calls to the value-store function (not bytes, not
     physical writes; each call stores 1–4 bytes). Diagnostic sensor
     `eeprom_store_calls`, `total_increasing`; its rate per day is the useful
     number.
   * R40965 = total EEPROM write failures (persisted). R40967 = failures
     since boot (RAM). Non-zero means a write was retried 10 times and never
     succeeded, leaving a wrong value in EEPROM. Any increase of R40965 or any
     non-zero R40967 → `WARNING` and `get_status.identity.eeprom_write_failed
     = true`.
6. `RI 43071` → uptime, seconds since boot, unsigned decimal. Read at every
   health check **and** with every fast poll (cheap, one register). If the
   new value is smaller than the previous one the display restarted:
   `WARNING`, `identity.last_restart` set, full health check, `status` event,
   and the heating controller re-pushes immediately. Exposed as sensor
   `uptime` (duration) and `get_status.identity.uptime` / `last_restart`.
7. The `settings` and `identity` tier sensors are read.

Results are kept in `State.identity` and exposed via `get_status.identity`
(`product`, `software_version`, `base_version`, `product_code`,
`clock_state`, `clock_drift`, `uptime`, `last_restart`, `eeprom_store_calls`,
`eeprom_write_failures`, `eeprom_write_failed`, `last_health_check`), which
the integration can show as diagnostic entities.

## Offline detection

A request that gets no reply within `pump.request_timeout` is retried
`pump.retries` times (default 2). After `pump.offline_after` seconds without
any successful reply the pump is `offline`: `status` event, controllers pause,
reconnect loop (close and re-open the port, first after 5 s, then doubling up
to every 30 s, since the display may have been power-cycled and the ACM
device re-enumerated). A port that cannot be opened at startup (missing
device, permission denied) is logged once as `pump not reachable on <port>`
and handled by the same loop; the server keeps running. First successful
`b` reply → `online`, full health check, full sensor poll, pending
degree-minute push.

## Console behaviour summary

| Item | Behaviour |
|---|---|
| Reply latency | first byte after about 0.1 s |
| `RT 1` | 28 fields plus a trailing `;`. Field N is BT N. Fields 13 and 16 repeat BT3 and BT14; unused slots read `0`, sensors that are not fitted read `-32768`. |
| `RI` limits | command text up to 61 characters and at most 10 registers. A longer line gets **no reply**, its reply arrives as the answer to the next command, and that next command is lost. An 11th register is silently dropped. |
| Unknown register id | `0 ` |
| `WI` | answers `1`. A write to a persisted register (such as R40940) costs one EEPROM store call (R40969 +1); without writes the counter stays flat. |
| `WM` | answers `1`. Writing the degree-minute RAM copy changes `RI 40940` immediately and costs **no** EEPROM store call, so degree minutes are written with `WM`. |
| Periodic increase timer | `RM 0x2066718C 3` reads `00 00 00` when no increase is running. |
| Pump-side DM reset | while heat demand R43110 is 0 the pump puts R40940 back to 0 within about a minute, so the controller makes no degree-minute writes in that state. |

### Clock

The display clock may be unset (after a power cycle it counts from
2010-01-01), so raw clock drift is not a useful health metric; instead the health check classifies the clock as
`unset` (year < 2020), `ok` (|drift| ≤ `pump.max_clock_drift`) or `drifting`,
exposed as `get_status.identity.clock_state`. The periodic hot water increase
only depends on the hour and the day counter, so it keeps working with an
unset clock. Setting the clock from the server is not implemented: it would
be a new write operation, and its console verb is unknown.

## Probe script

`scripts/probe_console.py` is **read-only by default**: it sends the read set
(`RV`, `b`, `QR`, `AI`, `RT 1`, `RI` lines of at most 9 registers).
`--write-test` adds the degree-minute write test (`RI 40940 40969`,
`WI 40940 -20`, `RI 40940 40969`, and the same read again 70 s later).
`--dry-run` prints the commands without opening the port. `--limits` sends
`RI` lines of 6, 7, 8, 9 registers, 10 and 11 registers on a short line, and
2 registers padded to 58–62 characters, each followed by an `RV` sync so a
swallowed command or shifted reply is visible. `--eeprom` reads the EEPROM
store counter for 3 minutes, then writes DM -20, -21, -22 with `WI` and reads
again. `--memwrite` writes -20, -256, +256 with `WM` to the DM RAM address,
reads back via `RI` and `RM`, checks the counter, and reads the
periodic-increase timer and flags (read only). Every `RI` reply is checked for
the expected token count.

Usage and a table of which modes write are in
[development.md](development.md#console-probe-script-scriptsprobe_consolepy).

## Interface contract

`pump/interface.py`; implemented by `SerialPumpClient` and
`SimulatedPumpClient`.

```python
class PumpClient(Protocol):
    async def start(self) -> None
    async def stop(self) -> None
    online: bool; port: str; error_count: int; last_ok: float | None          # properties
    async def read(self, register: int, *, priority=Priority.POLL) -> int                   # raw
    async def read_many(self, registers: Sequence[int], *, priority=Priority.POLL) -> list[int]  # chunked into RI lines of ri_batch_size
    async def read_temperatures(self, *, priority=Priority.POLL) -> list[int]               # RT 1, 28 raw values
    async def read_ram(self, address: int, length: int, *, priority=Priority.POLL) -> bytes # RM
    async def write(self, register: int, value: int, *, priority=Priority.COMMAND) -> None  # WI, expects "1", read-back
    async def write_ram(self, address: int, data: bytes, *, priority=Priority.COMMAND) -> None  # WM, expects "1", read-back
    async def identity(self, *, priority=Priority.CONTROL) -> Identity                     # b, RV, QR
    async def clock(self, *, priority=Priority.CONTROL) -> Clock                           # AI
    def on_state_change(self, callback) -> None                                             # online / offline
```
