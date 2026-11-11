# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow the
`version` in `pyproject.toml`. The wire protocol has its own version (`proto`,
see [docs/protocol.md](docs/protocol.md#versioning-rules)).

## [0.1.0] - unreleased

First public release. Tested on a Nibe F1226-6 R with display firmware 7994R7.

### Added

- Gateway between the Nibe service USB console and the Home Assistant
  integration [ha-nibe-service-usb](https://github.com/tumik/ha-nibe-service-usb):
  JSON-lines TCP API (`proto` 1) with mutual HMAC-SHA256 authentication and a
  fixed command allowlist.
- Sensor polling: a fast cycle every 60 s (`poll.fast_interval`) and a full
  cycle every 600 s (`poll.interval`), with settings hourly and an optional
  diagnostic tier. Only changes are pushed.
- Hot water comfort mode (`eco` / `normal` / `lux`) and an immediate trigger
  of the periodic hot water increase.
- Heating request `enable` / `disable` / `auto` through degree-minute RAM
  writes. The DM RAM address is verified at startup, no write happens while DM
  is exactly 0, and safety guards apply to `disable`.
- Watchdog: reverts to `auto` and the default hot water mode after 5 minutes
  without a heartbeat. It never reverts before the first heartbeat.
- mDNS discovery (`_nibe-usb._tcp.local.`) that re-registers when the host's
  addresses change.
- Optional local debug website (`--debug-website`), unauthenticated, on
  localhost by default.
- `simulate` mode with an in-memory pump. `probe` and `check-config`
  commands.
- `scripts/probe_console.py` console probe, read-only by default. Write
  tests are opt-in (`--write-test`, `--eeprom`, `--memwrite`).
- systemd unit, installation guide, CI (ruff and pytest on Python 3.11 to 3.13).
- More sensors:
  - settings: additional heat start and step difference of menu 4.9.3
    (`set_start_addheat`, `set_addheat_step_diff`), max diff for compressor
    and additional heat (`set_max_diff_comp`, `set_max_diff_addheat`) and the
    heat medium pump speed in hot water (`set_hw_pump_speed`);
  - counters: additional heat time factor of menu 3.3 (`addheat_time_factor`,
    `addheat_time_factor_hw`);
  - pump states of the heat medium and brine pumps (`gp1_state`, `gp2_state`);
  - periodic increase and temporary lux: `pi_days_until`, `pi_start_hour`,
    `tl_remaining`;
  - identity: `language`, and country and language as text (`country_name`,
    `language_name`).
- Sensor types `rl_seconds`, `rl_hour` and `rl_days_until` for registers that
  hold a time of day or a date. They are read with the `RL` console command;
  see [docs/sensors-and-polling.md](docs/sensors-and-polling.md).

### Changed

- `eeprom_write_failures` (R40965) is documented as what it is: a persisted
  count of EEPROM and clock bus jobs that ran out of tries, which can hold a
  large meaningless value from the factory. Only an increase is a warning;
  the log message now says "EEPROM/RTC job failures". See
  [docs/pump-interface.md](docs/pump-interface.md#startup--health-check-sequence).

### Fixed

- The pump console no longer hangs after a host reboot or cable replug. On
  firmware 7994R7 a USB bus reset while the console is listening leaves it
  deaf until the pump is restarted. The server now disarms the port when idle
  and before closing it (`pump.idle_disarm_after`, `pump.arm_delay`) and
  reports the state as `pump.armed` / `pump.last_disarm` in `status` and on
  the debug website; see
  [docs/pump-interface.md](docs/pump-interface.md#usb-bus-reset-hang-and-disarming).

### Security

- The auth key is never written to the log. Use `nibe-service-usb show-key`
  to display it.
