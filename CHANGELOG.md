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

### Security

- The auth key is never written to the log. Use `nibe-service-usb show-key`
  to display it.
