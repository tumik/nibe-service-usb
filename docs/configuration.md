# Configuration, data directory, CLI

## Files

| Path | Purpose |
|---|---|
| `config.toml` | Configuration. Optional, because every key has a default. Location: see below. |
| `<data_dir>/auth.key` | The 32-byte auth key as 64 hex characters, mode `0600`. Created on first use. |
| `<data_dir>/server_id` | A UUID created once. Home Assistant uses it as the device's unique id. |
| `sensors.yaml` | Sensor and alarm definitions. The package ships one; `poll.sensors_file` points to your own copy. |

**Config file lookup** (first match wins): the `--config PATH` option, then
the `NIBE_SERVICE_USB_CONFIG` environment variable, then
`/etc/nibe-service-usb/config.toml` if it exists. If none of these exist, the
built-in defaults are used.

**Data directory** (first match wins): `--data-dir PATH`, then
`NIBE_SERVICE_USB_DATA_DIR`, then `~/.local/share/nibe-service-usb`. The
systemd unit sets `NIBE_SERVICE_USB_DATA_DIR=/var/lib/nibe-service-usb` (see
[installation.md](installation.md)). The directory is created with mode
`0700` if it does not exist.

## Validation

The file is TOML with the sections shown below. Loading fails with a message
that names the dotted key in these cases:

- an unknown section or key (this catches typos and keys from older
  versions, e.g. `poll.control_interval`, which is now `poll.fast_interval`);
- a value of the wrong type (`true`/`false` for booleans; integers where an
  integer is required);
- a value out of range (see the tables).

Run `nibe-service-usb check-config` to validate a file. It prints the
effective values (defaults merged with your file) and a summary of
`sensors.yaml`.

TOML has no null. Two keys therefore use a special value:
`defaults.hot_water_mode = ""` means "leave untouched", and `false` disables
a `safety.min_*` guard.

## Reference

[`config.example.toml`](../config.example.toml) lists every key with its
default and a short comment. The tables below add the allowed ranges.

### `[pump]`: service USB console

| Key | Default | Allowed | Meaning |
|---|---|---|---|
| `port` | `"/dev/ttyACM0"` | path | Serial device. Prefer `/dev/serial/by-id/...` if there are several ACM devices. |
| `baudrate` | `115200` | > 0 | Ignored by CDC-ACM. |
| `rts` / `dtr` | `true` / `true` | bool | Control lines asserted on open. The console needs RTS. |
| `ri_batch_size` | `9` | 1..10 | Registers per `RI` line. |
| `max_line_length` | `61` | 16..61 | Hard limit per command line. |
| `request_timeout` | `3.0` | > 0 | Seconds per reply. |
| `retries` | `2` | ≥ 0 | Retries per request. |
| `offline_after` | `60` | > 0 | Seconds without any reply until the pump counts as offline. |
| `daily_check_time` | `"03:30"` | `"HH:MM"` | Daily health check, local time. |
| `max_clock_drift` | `120` | ≥ 0 | Seconds. The clock is reported as `unset` (year < 2020), `ok` or `drifting`. |

### `[server]`: TCP API for Home Assistant

| Key | Default | Allowed | Meaning |
|---|---|---|---|
| `listen_host` | `"0.0.0.0"` | address | Bind address. A specific address is also the only one advertised over mDNS. |
| `listen_port` | `9477` | 0..65535 | `0` = random (used by `simulate`). |
| `max_clients` | `4` | ≥ 1 | Authenticated connections at the same time (twice as many sockets in total). |
| `idle_timeout` | `600` | > 0 | Seconds of silence before an authenticated connection is closed. |

### `[discovery]`: mDNS

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Announce `_nibe-usb._tcp.local.`. |
| `name` | `"Nibe heat pump"` | Shown in the Home Assistant discovery card. The instance name is `<name>-<first 8 hex of server_id>`. |
| `model` | `""` | Optional `model` TXT entry. |

### `[watchdog]` and `[defaults]`

| Key | Default | Allowed | Meaning |
|---|---|---|---|
| `watchdog.timeout` | `300` | ≥ 120 | Seconds without a heartbeat before reverting. Home Assistant pings every 60 s. |
| `defaults.hot_water_mode` | `"normal"` | `eco`/`normal`/`lux`/`""` | Hot water mode written when the watchdog trips. `""` leaves it alone. The heating request always reverts to `auto`. |

### `[poll]`: sensor polling

| Key | Default | Allowed | Meaning |
|---|---|---|---|
| `interval` | `600` | ≥ 60 | Seconds between full polls. |
| `fast_interval` | `60` | 10..`interval` | Seconds between fast polls (all core sensors except the three slow counters, plus alarms and uptime). |
| `settings_every` | `6` | ≥ 1 | The settings tier is read every Nth full poll. |
| `sensors_file` | `""` | path | Your own `sensors.yaml`. `""` uses the packaged file. |
| `diagnostic` | `false` | bool | Also poll the diagnostic tier (in full polls). |

### `[heating]`: degree-minute control

All values in raw units (raw = 10 × the DM shown on the display). See
[heating-control.md](heating-control.md).

| Key | Default | Allowed | Meaning |
|---|---|---|---|
| `dm_push_interval` | `600` | > 0 | Seconds between evaluations while `enable`/`disable` is active. |
| `dm_disable_value` | `-1` | within [`dm_min`, `dm_max`] | Value written by `disable`. |
| `dm_start_threshold` | `0` | int | `0` = R47206 × 10 from the pump. Any other value overrides it. |
| `dm_enable_margin` | `1` | ≥ 0 | `enable` target = threshold + margin. |
| `dm_min` / `dm_max` | `-15000` / `0` | ≥ -15000 / ≤ 0, min ≤ max | Absolute bounds for anything written. |
| `dm_write_method` | `"ram"` | `ram` / `register` | `ram` = `WM` to `dm_ram_address` (no EEPROM). `register` = `WI 40940` (one EEPROM store per write). |
| `dm_ram_address` | `"0x2038af18"` | 32-bit, hex string or int | RAM copy of R40940 on F1226-6 R / 7994R7. Verified against `RI 40940` at startup. |
| `dm_write_policy` | `"always"` | `always` / `threshold` | `threshold` = economy mode for `disable`. |
| `dm_guard_fraction` | `0.5` | 0..1 | Used by the `threshold` policy. |
| `max_writes_per_day` | `100` | ≥ 1 | Daily cap, `register` method only. |

### `[safety]`: guards for `disable`

| Key | Default | Meaning |
|---|---|---|
| `min_room_temp` | unset | BT50 room temperature. Set it only if a BT50 is fitted; without one the guard reports `stale_data`. |
| `min_supply_temp` | `20.0` | BT2 flow temperature. |
| `min_outdoor_temp` | `-15.0` | BT1 outdoor temperature. |
| `max_sensor_age` | `900` (> 0) | Seconds. Older guard readings count as stale. |
| `hysteresis` | `1.0` (≥ 0) | °C above the limit before a tripped guard clears. |

### `[debug_website]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | `run --debug-website [HOST:]PORT` also enables it. |
| `host` | `"127.0.0.1"` | **No authentication.** Anything other than loopback exposes a control page. |
| `port` | `0` | 0..65535. `0` = free port, logged at startup. |
| `feeds_watchdog` | `false` | Whether commands from the page count as a Home Assistant heartbeat. |

### `[log]`

| Key | Default | Meaning |
|---|---|---|
| `level` | `"info"` | `debug`/`info`/`warning`/`error`/`critical`, case-insensitive. `debug` logs raw console traffic. |

## CLI

```
nibe-service-usb [--version] COMMAND [--config PATH] [--data-dir PATH] [--log-level LEVEL] ...

  run          [--debug-website [HOST:]PORT]   run the gateway
  simulate     [--debug-website [HOST:]PORT]   run against the in-memory pump simulator
  probe                                        read identity, clock and a few registers (read-only), then exit
  check-config                                 validate config and sensors.yaml, print effective values
  show-key                                     print the auth key (creates it if missing)
  rotate-key                                   write a new auth key; restart a running server afterwards
```

- The options come **after** the command.
- `--debug-website PORT` keeps `debug_website.host` from the config (default
  `127.0.0.1`). `HOST:PORT` overrides it. `PORT` `0` picks a free port.
- `--log-level` overrides `log.level`.
- `simulate` turns mDNS off and uses a random TCP port unless the config
  file sets `discovery.enabled` or `server.listen_port`.
- `probe` exits with status 1 if the pump does not come online within 10 s.
- `show-key` prints the key as 64 hex characters and as dash-grouped base32.
  Both are the same 32 bytes.
- A running server does not notice `rotate-key`; it keeps the old key until
  it is restarted.

The systemd unit and the permanent install are described in
[installation.md](installation.md).
