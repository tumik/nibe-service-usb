# Heating control via degree minutes

## Background

The pump integrates the difference between calculated and actual supply
temperature into a **degree-minute (DM) counter**. Negative DM means heat
deficit. The compressor starts when DM falls below a start threshold
(typically **-60**, pump setting) and the pump raises DM back as the deficit is
served. Additional heat (electric) engages at a much lower threshold.

Writing the DM register does not switch heating on or off directly. It only
moves the counter so that the pump's own logic, which runs about once a minute,
makes the decision from a new starting point. That is exactly why this approach
is safe to expose: the pump keeps full authority.

## Register facts (F1226-6 R)

* DM register **R40940**, s32, **raw = 10 × displayed DM** (the display
  scale is not yet compared with the screen; see the open question at the end). Everything below is in raw units; the HA sensor shows raw/10.
* Compressor starts at `R40940 < R47206 × 10`. R47206 "start compressor" is
  a plain integer in displayed DM (default -60; e.g. -200), so the raw start
  threshold is `R47206 × 10` (-600; -2000). Stops at `R40940 >= 0`. Electric addition step 1 at
  raw -4600 with default settings.
* Pump clamps: lower `(R47206 − R48072) × 10 + 10`, upper `+1000`.
* Integrated **once per minute** from `control flow (R41788 = BT2) − calculated
  flow (R43009)`.
* **Reset to 0 by the pump** whenever heat demand R43110 = 0 (avg outdoor ≥
  stop heating, AUX block heating, …).
* **Not integrated during a hot water charge** on the test unit (BT25 absent): the
  value is frozen or only falls while QN10 is in hot water position.
* The compressor has a 20 min start inhibit after a start and ≥ 5 min after a
  stop (R43405), so a push can take that long to have a visible effect.

Consequences for the controller: the start threshold is read from R47206 at
every push (config may override), the DM read before a push is also used to
detect the pump-side reset, and no DM is written while `op_priority` = hot
water or `heat_demand` = 0 (the write would be undone or meaningless).

## Modes

`HeatingController` is a tri-state machine: `auto` | `enable` | `disable`.
Set via `set_heating_request`, reverted to `auto` by the watchdog or on server
restart.

| Mode | Every push | Effect on pump |
|---|---|---|
| `auto` | nothing | Pump runs its own DM integration. |
| `disable` | write `DM = dm_disable_value` (default **-1 raw**, i.e. -0.1 displayed) | If the pump already considers the house warm enough, DM will climb past 0 and the compressor stops; it cannot restart until DM drops to the start threshold again, which takes the deficit time from -1 → -60 instead of from wherever it was. If the house is actually colder than the pump's own target, DM just keeps falling from -1 and heating starts normally — **no-op** in that case. |
| `enable` | write `DM = R47206 × 10 + dm_enable_margin` (margin **1 raw**: with R47206 = -200 the target is **-1999**; with the default -60 it is -599) | If the house is at or below the pump's target, DM crosses the threshold at the next 1-minute integration step and the compressor starts. If the house is warmer than the pump's target, DM rises from the target and nothing starts — **no-op** in that case. |

The server never changes the pump's heating curve, setpoints or operating mode.

## Push schedule

* On `set_heating_request(enable|disable)`: evaluate and push **immediately**,
  then every `dm_push_interval` (default **600 s**).
* Also push (if due or on reconnect) when the pump comes back online after
  being offline, so that a USB hiccup does not silently drop the request.
* On `set_heating_request(auto)` or watchdog revert: cancel the timer, write
  nothing.

## Decision table for one push

Each push does a `CONTROL`-priority read of DM, heat demand, operating
priority, compressor state, start inhibit and R47206 (for `disable`, preceded
by a fresh read of the guard sensors), then applies the rules in this order;
the first that matches decides:

1. pump offline → skip (`pump_offline`), retried when online;
2. DM RAM address check failed → skip (`dm_address_invalid`);
3. `disable` only: a safety guard is violated → skip (`safety_*` / `stale_data`);
4. current DM is exactly 0 → skip (`dm_zero`);
5. heat demand = 0 or hot water priority → skip, no write;
6. the direction rule in the table below;
7. `register` write method only: daily budget used up → skip (`write_budget`).

| Mode | Condition | Action |
|---|---|---|
| `disable` | any guard violated | skip write; `active=false`, `suspended_reason=<guard>`; `status` event (only on transition) |
| `disable` | `current_dm > dm_disable_value` (e.g. DM = +20, pump already stopped) | **skip write**, nothing to gain; `active=true` |
| `disable` | `current_dm <= dm_disable_value` | write `dm_disable_value` (-1) |
| `enable` | `current_dm > target` (e.g. raw -100 → -1999) | write `target` (-1999) |
| `enable` | `current_dm <= target` (already below -1999, e.g. compressor running at -2500) | **skip write** — raising DM here would delay or interrupt heating, the opposite of the request |
| `enable` | `current_dm > target` while the compressor is running or in start inhibit | **write `target`** — same rule as `disable`: the compressor state does not matter, a running compressor is kept running by re-pushing the low target |
| any | **current DM is exactly 0** | **never write** (`suspended_reason = "dm_zero"`): a counter sitting at 0 means the pump itself is holding it there because heating is blocked by one of its rules (stop heating, AUX block heating, operating mode, …). Checked before any write decision. |
| any | pump offline | skip; retried when online |

"Write only when it moves DM in the requested direction" is the core rule. It
guarantees that `enable` can never reduce heating and `disable` can never
increase it beyond what the pump would do by itself, and it avoids needless
writes to the pump. The probe's before/after reads of the EEPROM store
counter R40969 showed that a `WI` write costs one EEPROM store call and a
`WM` RAM write costs none (see `pump-interface.md`), which is why RAM writes
are the default.

Every write is followed by a read-back (`RM` of the RAM address, or `RI` of
the register). A write that is not acknowledged with `1`, or whose read-back
differs from the value written, → `write_failed` logged at `WARNING` and
`suspended_reason = "write_failed"` until the next evaluation. After a
successful write, R40940 is read once more and published as `current_dm`
and as the `degree_minutes` sensor (the pump may already have integrated or
reset it).

## How DM is written, and the write budget

Degree minutes are written with `WM 0x2038af18 <s32 LE>` to the RAM copy of
R40940 (measured: value visible via `RI 40940` immediately, **no EEPROM
store**). `WI 40940` would cost one EEPROM store call per write (measured
twice) and is kept only as the `dm_write_method = "register"` fallback.

With RAM writes the user's original schedule applies as specified: every
`dm_push_interval` (10 min) the controller evaluates and writes whenever the
value moves DM in the requested direction. Remaining rules:

1. **Pump-side reset (measured: -22 → 0 within 60 s).** When heat demand
   R43110 = 0 the pump holds DM at 0: both modes are no-ops and nothing is
   written. Same during a hot water charge (priority 20): DM is frozen.
2. **Both directions use one rule:** write whenever the write moves DM
   towards the request, regardless of the compressor state (running,
   starting, stopping or in start inhibit). `enable` therefore keeps a
   running compressor running until the mode is changed; Home Assistant (or
   the watchdog after 300 s without heartbeat) ends it.
3. `dm_write_policy = "threshold"` is an optional economy mode (write in
   `disable` only when DM is, or will be before the next evaluation, past
   `dm_guard_fraction` of the start threshold). Default is `"always"` now
   that writes are free.
4. If `dm_write_method = "register"` is in use, `max_writes_per_day`
   (default 100) caps EEPROM stores and `suspended_reason = "write_budget"`
   is reported when hit. Not applied to RAM writes.
5. At startup the server verifies `heating.dm_ram_address` by checking that
   `RM` of it decodes to the value `RI 40940` returns; mismatch → the
   controller refuses to run (`suspended_reason = "dm_address_invalid"`)
   until the config is fixed.

## Safety guards (apply to `disable` only)

`disable` holds heating off only as long as the house stays within configured
limits. Guards are checked against the **latest sensor values** (a dedicated
`CONTROL` read of the guard sensors is done right before each push so the data
is at most seconds old, not up to 10 minutes). All configurable; a guard with
no configured limit is inactive.

| Guard | Config key | Default | Suspended when |
|---|---|---|---|
| Room temperature | `safety.min_room_temp` | unset | BT50 (R40033) below limit; set it only if a BT50 room sensor is fitted |
| Supply (flow) temperature | `safety.min_supply_temp` | `20.0` | BT2 (R40008) below limit |
| Outdoor temperature | `safety.min_outdoor_temp` | `-15.0` | BT1 (R40004) below limit (very cold: let the pump decide) |
| Stale data | `safety.max_sensor_age` | `900` s | a guard sensor value is missing or older than this (e.g. pump was offline); reason `stale_data` |

Hysteresis: a guard clears only when the value is back above
`limit + safety.hysteresis` (default `1.0 °C`), so the controller does not
flap around the limit.

While suspended the mode remains `disable` (HA's intent is remembered) but no
writes happen; the pump heats as it sees fit. When the guard clears, pushes
resume at the next scheduled push. Every suspend/resume transition is a
`status` event and a `WARNING` log line.

`enable` has no guards: asking for heat can at most cause the pump to run when
it would otherwise have waited; the pump's own maximum limits still apply.

## Configuration

```toml
[heating]                    # raw units: raw = 10 x displayed DM
dm_push_interval = 600       # seconds
dm_disable_value = -1        # raw
dm_start_threshold = 0       # 0 = use R47206 x 10 read from the pump; non-zero = override (raw)
dm_enable_margin = 1         # raw; enable target = R47206 x 10 + margin (e.g. -2000 + 1 = -1999)
dm_write_method = "ram"      # ram (WM 0x2038af18, no EEPROM) | register (WI 40940, one EEPROM store per write)
dm_ram_address = "0x2038af18" # RAM copy of R40940 (F1226-6 R, 7994R7); verified against RI 40940 at startup
dm_write_policy = "always"   # always | threshold (economy mode)
dm_guard_fraction = 0.5      # threshold policy only
max_writes_per_day = 100     # register method only
dm_min = -15000              # absolute lower bound the server will ever write (sanity)
dm_max = 0                   # absolute upper bound the server will ever write

[safety]
# min_room_temp = 18.0       # off by default; needs a BT50 room sensor
min_supply_temp = 20.0
min_outdoor_temp = -15.0
max_sensor_age = 900
hysteresis = 1.0
```

The start threshold is R47206 (readable, settings tier). The server reads it
on every push and uses `R47206 × 10` unless `dm_start_threshold` is set
non-zero. This keeps `enable` correct if the user changes the pump setting
from the panel.

## Interaction with other features

* The sensor poller exposes `degree_minutes` as a normal sensor, so HA can
  show what the controller is doing. A push also updates that sensor value
  immediately (from the read-back).
* `get_status.heating` exposes `mode`, `active`, `suspended_reason`,
  `last_push`, `next_push`, `target_dm`, `current_dm`.
* Hot water production is independent of space-heating DM on Nibe pumps;
  `disable` does not affect hot water.

## Suspended reasons

`get_status.heating.suspended_reason` (and the `set_heating_request` result)
is `null` while the controller is active, otherwise one of:

| Reason | Meaning |
|---|---|
| `pump_offline` | pump not reachable; re-evaluated when it is back |
| `dm_address_invalid` | `heating.dm_ram_address` did not match `RI 40940` at startup; no DM writes until fixed |
| `safety_room_temp` / `safety_supply_temp` / `safety_outdoor_temp` | a guard is violated (`disable` only) |
| `stale_data` | a guard sensor reading is missing or too old (`disable` only) |
| `dm_zero` | the pump holds DM at exactly 0 (heating blocked by the pump) |
| `write_budget` | `register` method: `max_writes_per_day` reached |
| `write_failed` | the last write was not acknowledged or read back differently |

## Open questions

* Display scale: the factor 10 between `RI 40940` and the DM shown on the
  display comes from the firmware code; it has not yet been compared with the
  screen on a running unit.

The test unit has no room sensor (BT50), so the `min_room_temp` guard is off
by default; the supply guard uses BT2 and the outdoor guard BT1.
