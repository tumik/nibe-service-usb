# Sensor selection for Home Assistant

Which values of the F1226-6 R the server exposes, and how. Registers are read-only here; scale is applied by the server so HA
receives final values. The authoritative list is
`src/nibe_service_usb/sensors.yaml`; keys below are its sensor names.

Tiers:

* **core** – polled every minute (`poll.fast_interval`; the three slow counters
  every 10 min with the full cycle), pushed on change. Aim: what a dashboard
  and automations need.
* **settings** – polled hourly and at every health check. They change only
  when a person changes them; exposed so automations and the heating
  controller see the real limits.
* **identity** – read by the health check: at startup, after every
  reconnect or display restart, and daily. Diagnostic entities.
* **diagnostic** – opt-in (`poll.diagnostic = true`), polled in the full
  10-minute cycle. Useful for debugging, noise for a normal dashboard.
* **excluded** – not exposed, with the reason.

## Core (40 values)

### Temperatures (one `RT 1` line, fallback `RI`)

| key | reg | HA | note |
|---|---|---|---|
| `bt1_outdoor` | 40004 | sensor, temperature | |
| `bt2_flow` | 40008 | sensor, temperature | = control flow on the test unit (no BT25) |
| `bt3_return` | 40012 | sensor, temperature | compressor limit sensor (alarm 163 at ≥ 56 °C) |
| `bt6_hw_charge` | 40014 | sensor, temperature | hot water **control** sensor |
| `bt7_hw_top` | 40013 | sensor, temperature | |
| `bt10_brine_in` | 40015 | sensor, temperature | |
| `bt11_brine_out` | 40016 | sensor, temperature | |
| `bt12_cond_out` | 40017 | sensor, temperature | alarm 162 at > 63 °C |
| `bt14_hot_gas` | 40018 | sensor, temperature | alarm 155 at > 135 °C |
| `bt15_liquid` | 40019 | sensor, temperature | |
| `bt17_suction` | 40022 | sensor, temperature | |
| `avg_outdoor` | 40067 | sensor, temperature | decides heating on/off vs `set_stop_heating` |
| `calc_flow` | 43009 | sensor, temperature | heating curve output; DM integrates `bt2_flow - calc_flow` |

BT25 (40071) and BT50 (40033) are not fitted; `sensors.yaml` lists them with
`enabled: false` so they can be switched on if a sensor is ever added.

### Control state

| key | reg | type | HA | note |
|---|---|---|---|---|
| `degree_minutes` | 40940 | s32, ×0.1 | sensor | published in displayed DM (raw/10); the controller works in raw |
| `op_priority` | 43086 | enum 10 off / 20 hot_water / 30 heating | sensor enum | |
| `heat_demand` | 43110 | 0/1 | binary_sensor | 0 ⇒ DM is reset to 0 by the pump; the heating controller must not fight this |
| `compressor_fsm` | 43427 | enum 20 idle / 40 starting / 60 running / 100 stopping | sensor enum | |
| `compressor_running` | 43435 | 0/1 | binary_sensor, running | relay command |
| `compressor_status` | 43443 | enum 5 stopped / 6 running / 7 soft_start / fault codes | sensor enum + binary problem | |
| `compressor_inhibit` | 43405 | u8 min | sensor, duration | explains "wanted but not running" |
| `gp1_speed` | 43437 | % | sensor | heat medium pump |
| `gp2_speed` | 43439 | % | sensor | brine pump |
| `gp1_state` | 43431 | enum 10 off / 15 starting / 20 on / 40 ten_day_mode / 80 calibration | sensor | heat medium pump state: 10 = stopped (speed 0), 15 = step of about one second between 10 and 20 (speed 0), 20 = running, 40 = brine pump only, with operating mode R47139 = 30 (10 days continuous) for as long as the counter R43042 is above 0, 80 = forced run at 100 %. 50 = 120 s run after a high condenser temperature info (162/163); it has no name and shows as `unknown_50` |
| `gp2_state` | 43433 | as `gp1_state` | sensor | brine pump |
| `qn10_hot_water` | 43481 | 0/1 | binary_sensor | valve in hot water position |
| `hw_mode` | 43109 | enum -1 off / 0 eco / 1 normal / 2 lux / 3 periodic | sensor enum | effective mode |
| `hw_comfort` | 47041 | enum 0 eco / 1 normal / 2 lux | sensor enum (also the value the `set_hot_water_mode` command changes) | |
| `hw_reason` | 40313 | enum (0 comfort … 12) | sensor enum | why `hw_mode` is what it is |
| `tl_selection` | 48132 | enum 0 off / 1 3h / 2 6h / 3 12h / 4 one_time | sensor enum | temporary lux from the panel |
| `el_power_kw` | 43084 | ×0.01 kW | sensor, power | nominal electrical addition |
| `el_step_actual` | 43091 | u8 | sensor | |
| `frost_mode` | 43013 | 0/1 | binary_sensor | freeze protection (info 180) |
| `any_alarm` | 43012 | 0/1 | binary_sensor, problem | general (red) alarm |
| `pi_failure` | 45111 | 0/1 | binary_sensor, problem | periodic increase failed (info 181) |
| `pi_running` | `RM 0x2066718C 3` ≠ 0 | 0/1 | binary_sensor, running | periodic increase in progress |
| `pi_remaining` | same, h/m/s → seconds | s | sensor, duration | time the run can still last |
| `pi_days_until` | `RL 44259` (date) minus the pump's own date | d | sensor, duration | days until the next periodic increase; counted down at midnight, equals the period right after a run; the run starts at `pi_start_hour` on day 0. Read every 10 minutes |
| `tl_remaining` | `RL 47526` | s | sensor, duration | temporary lux time left, 0 when not running |
| `pi_hold` | 43014 | 0/1 | binary_sensor | target reached, 1 h hold phase (only meaningful while running) |
| `pi_enabled` | 47050 | 0/1 | binary_sensor | periodic increase enable bit (the trigger toggles it) |
| `active_alarms` | 53 flag regs (6 `RI` lines of 9) | list of `{number, title}` | sensor (state = count, attribute = list) | one entity instead of 53 binary sensors; polled with core because info-class flags do not raise `any_alarm` |

### Counters (core tier, but read only in the full 10-minute cycle)

| key | reg | HA |
|---|---|---|
| `comp_starts` | 43416 | sensor, total_increasing |
| `comp_hours` | 43420 | sensor, duration, total_increasing |
| `comp_hours_hw` | 43424 | sensor, duration, total_increasing |
| `addheat_time_factor` | 43081 | sensor, ×0.1 h, total_increasing; menu 3.3 "time factor" |
| `addheat_time_factor_hw` | 43239 | sensor, ×0.1 h, total_increasing; menu 3.3 "- of which hot water" |

## Settings (hourly)

| key | reg | scale | note |
|---|---|---|---|
| `set_start_comp` | 47206 | DM (plain integer, e.g. -200) | **used by the heating controller**: raw threshold = value × 10, enable target = value × 10 + 1 |
| `set_start_addheat` | 48072 | DM (plain integer, default 400) | menu 4.9.3 "start diff additional heat": addition step 1 starts at `(set_start_comp − value) × 10` raw |
| `set_addheat_step_diff` | 47209 | DM (plain integer, default 100) | menu 4.9.3 "diff. between additional steps" |
| `set_stop_heating` | 47375 | ×0.1 °C | |
| `set_stop_addheat` | 47376 | ×0.1 °C | |
| `set_max_diff_comp` | 47378 | ×0.1 K | menu 5.1.3 "max diff compressor" |
| `set_max_diff_addheat` | 47379 | ×0.1 K | menu 5.1.3 "max diff addition" |
| `set_hw_pump_speed` | 47413 | % | menu 5.1.11, GP1 speed during a hot water charge |
| `set_op_mode` | 47137 | enum 0 auto / 1 manual / 2 add_heat_only | mode 2 blocks the compressor; `enable` request is pointless then |
| `set_el_max` | 47212 | ×0.01 kW | |
| `set_curve` | 47007 | | |
| `set_curve_offset` | 47011 | | |
| `set_min_flow` | 47015 | ×0.1 °C | |
| `set_max_flow` | 47019 | ×0.1 °C | |
| `set_hw_start_eco/normal/lux` | 47045 / 47044 / 47043 | ×0.1 °C | |
| `set_hw_stop_eco/normal/lux` | 47049 / 47048 / 47047 | ×0.1 °C | |
| `pi_period` | 47051 | days | |
| `pi_start_hour` | `RL 47052` (seconds since midnight) / 3600 | h | hour of the day at which a due periodic increase starts |
| `pi_stop_temp` | 47046 | ×0.1 °C | |
| `set_filter_time` | 47377 | h | |

## Identity (health check: startup / reconnect / daily)

Sensors in `sensors.yaml` (tier `identity`):

| key | source | HA |
|---|---|---|
| `base_version` | `RI 32041` (cross-checked against `RV` field 2) | diagnostic sensor |
| `country` | 48745 | diagnostic sensor (number) |
| `country_name` | 48745 | diagnostic sensor, enum: ISO code (`AX`, `FI`, ...), `other` (0), `not_selected` (52) |
| `language` | 47131 | diagnostic sensor (language pack index, 5 = Finnish) |
| `language_name` | 47131 | diagnostic sensor, enum: name in the pump's language menu (`suomi`, `svenska`, ...) |
| `uptime` | 43071 (u32 s; also read in every fast poll) | diagnostic sensor, duration; a decrease = restart |
| `eeprom_store_calls` | 40969 | diagnostic sensor, total_increasing (calls to the store function, 1–4 bytes each) |
| `eeprom_write_failures` | 40965 | diagnostic sensor, total_increasing. Persisted count of EEPROM/RTC bus jobs that used all 10 tries (not only writes, see [pump-interface.md](pump-interface.md#startup--health-check-sequence)) |
| `eeprom_write_failures_boot` | 40967 | diagnostic sensor; the same count since the display booted (RAM) |

Fields of `get_status.identity` (not sensors; the integration can expose them
as diagnostic entities):

| key | source | meaning |
|---|---|---|
| `product` | `b` | e.g. "F1226-6 R" |
| `software_version` | `b` | e.g. "7994R7" |
| `base_version` | `RV` field 2 | PCA base version |
| `product_code` | `QR` | 14-digit product code |
| `clock_state` | derived from `AI` | `unset` / `ok` / `drifting` (test unit: unset, counts from 2010-01-01 since boot) |
| `clock_drift` | `AI` Unixtime − host time | seconds; `null` while `clock_state` is `unset` |
| `uptime`, `last_restart` | 43071 | seconds since boot; timestamp of the last detected restart |
| `eeprom_store_calls`, `eeprom_write_failures` | 40969, 40965 | as above |
| `eeprom_write_failed` | derived: 40967 ≠ 0 or 40965 increased | problem flag |
| `last_health_check` | | timestamp |

## Diagnostic (opt-in)

`comp_perm` 43112, `add_perm` 43113, `hw_addheat_state` 31003,
`prio_seconds` 43026, `compressor_reason` 43102, `compressor_prerun` 43409,
`relays_pca_base` 43514, `relays_aa1` 43516, `relays_flags` 43518,
`el_step_target` 43200, `alarm_log_index` 45119,
`pca_status_byte` 32040, `control_flow` 41788, `curve_outdoor` 40185,
`req_hw_comp` 43201, `req_heat_comp` 43203, `req_heat_el` 43204,
AUX: `aux1_selector` 47411, `aux2_selector` 47410, `aux1_raw` 32004,
`aux2_raw` 32005, block flags 43151 / 40814 / 43150 / 43523 / 43153 / 43161 /
43483, `sg_ready` 44874, protection inputs `hp_input` 40052, `lp_input`
40053, `tb_input` 40054 (their alarms are already in `active_alarms`).

## Excluded

* Registers that are not valid on this model (inverter, energy meters,
  AZ10 pages, PCA Input): they read as 0 and would mislead.
* `gp1_duty` / `gp2_duty`: no register on the console.
* `alarm_log_entry`: needs `RM` of a 68-byte record; not implemented.
* `el_status` text renderer: no register.
* Per-alarm binary sensors: folded into `active_alarms`.

## Poll budget per cycle

`RI` lines carry at most 9 registers (measured: line ≤ 61 characters, ≤ 10
registers).

| lines | content |
|---|---|
| 1 | `RT 1` (11 BT temperatures) |
| 3 | `RI` × 9: control state, `avg_outdoor`, `calc_flow`, uptime (≈ 24 regs); plus the 3 counters in the full cycle |
| 1 | `RM 0x2066718C 3` periodic increase timer |
| 6 | `RI` alarm flags (53 regs) |
| 2 | `RI` settings (18 regs), only in the hourly full cycle and at health checks |
| 4 | `RI` diagnostic (32 regs), only in full cycles with `poll.diagnostic = true` |

About 11 console lines per fast cycle (every minute), each answered in
≈ 0.1 s, so 1–2 s of serial time per minute. `COMMAND`/`CONTROL` requests slot
in between lines.
