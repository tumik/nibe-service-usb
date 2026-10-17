# nibe-service-usb

**Remote monitoring and control for Nibe heat pumps that have no official way
to do it.** Some Nibe models, such as the **F1226**, have no network
interface, no Modbus interface, and no Modbus or internet add-on you can buy
for them. This project connects them to **Home Assistant** anyway, through
the pump's **service USB port**.

**No heat pump add-ons needed: just a USB cable and a small Linux computer**
next to the pump (a Raspberry Pi is enough). The gateway reads the pump's operating
data (all temperatures, compressor and pump state,
degree minutes, hot water mode, alarms, counters and settings) and lets Home
Assistant control the pump: hot water comfort mode (eco / normal / lux), an
immediate periodic hot water increase (anti-legionella), and holding space
heating on or off. Everything stays local: no cloud account, no Modbus module,
no changes to the pump's wiring.

**Home Assistant side:** install the companion integration
**[tumik/ha-nibe-service-usb](https://github.com/tumik/ha-nibe-service-usb)**.
This repository is only the gateway server.

> [!WARNING]
> **Read this before you connect anything. You use this software entirely at
> your own risk and responsibility.**
>
> - **Tested on one unit only:** a Nibe **F1226-6 R** with display firmware
>   **7994R7**. It may not work on other models or firmware versions, and it
>   may misbehave on them. Register numbers and especially RAM addresses can
>   differ.
> - **The service USB port is a service-only interface.** It is meant for
>   Nibe service technicians, it is undocumented, and it accepts dangerous
>   commands that can break the heat pump. This server only sends a fixed,
>   allow-listed set of commands, but the port itself has no such protection.
>   Do not type commands into it yourself unless you know exactly what they do.
> - **Writing to a wrong register or memory address could permanently brick
>   the heat pump.** At startup the server checks that the configured
>   degree-minute RAM address (`heating.dm_ram_address`) holds the same value
>   as register 40940 and refuses to write degree minutes if it does not. That
>   check reduces the risk; it does not remove it.
> - **Some writes are stored in the pump's EEPROM, which wears out.** Each
>   hot water comfort change costs one EEPROM write, and each periodic hot
>   water increase trigger costs two. Degree-minute writes for the heating
>   request go to RAM only. Be careful with automations that change values
>   regularly: a hot water mode switched several times a day, every day, adds
>   EEPROM wear that the pump was not designed for and may wear it out
>   prematurely. The gateway exposes the EEPROM write counter so you can watch
>   it.
> - **Nibe does not support this project.** Watch the pump closely after
>   installing it, and do not use this software if you cannot accept these
>   risks.

## Features

- **Sensors.** About 40 core values (temperatures, degree minutes, compressor
  and pump states, hot water state, alarms) are read every minute, and slow
  counters every 10 minutes. Settings
  are read hourly. Diagnostic values are optional. Only changes are pushed to
  Home Assistant.
- **Hot water.** Set the comfort mode (`eco` / `normal` / `lux`) and start
  the periodic hot water increase (anti-legionella cycle) right away.
- **Heating request.** `enable` / `disable` / `auto`. The gateway moves the
  pump's degree-minute counter every 10 minutes so the pump's own control logic
  starts or holds off the compressor. The pump still makes the final
  decision. See [docs/heating-control.md](docs/heating-control.md).
- **Safety.** `disable` is suspended when the supply or outdoor temperature
  (and optionally the room temperature) drops below a limit. The server
  never writes when the counter is exactly 0, which means the pump itself is
  blocking heating. Every write is bounded, allowlisted and read back.
- **Watchdog.** If Home Assistant stops sending heartbeats for 5 minutes,
  the heating request goes back to `auto` and hot water goes back to a
  configured default.
- **Secure by design.** The TCP API has no generic register access. Clients
  use a mutual HMAC challenge/response with a 32-byte key that never crosses
  the network.
- **Auto-discovery** over mDNS (`_nibe-usb._tcp.local.`). You can also add the
  gateway by hand with host and port.
- **Debug website** (optional, off by default): a local web page with every
  value updating live and buttons for the same commands Home Assistant has.
- **Simulator mode** for trying it out or developing without a pump.

### Supported operations (the complete allowlist)

| Operation | Notes |
|---|---|
| Subscribe to sensors | Snapshot with metadata, then only changed values. |
| Set hot water comfort mode | `eco` / `normal` / `lux`, read back from the pump. |
| Trigger hot water periodic increase | Starts the anti-legionella cycle now. Limited to once per 10 min. |
| Request heating `enable` / `disable` / `auto` | Kept until changed, until the watchdog trips or until the server restarts. |
| Ping / status / refresh | Heartbeat, diagnostics, forced poll. |

Nothing else can be done through the gateway. Adding an operation requires a
code change.

## How it fits together

```
┌────────────────────────┐   TCP 9477, JSON lines    ┌──────────────────┐   USB (CDC-ACM)    ┌──────────────────┐
│ Home Assistant         │   HMAC auth, mDNS         │ nibe-service-usb │   service console  │ Nibe heat pump   │
│ + ha-nibe-service-usb  │ <───────────────────────> │ (e.g. Raspberry  │ <────────────────> │ display unit,    │
│   integration          │   sensors ↓  commands ↑   │  Pi, systemd)    │   /dev/ttyACM0     │ USB type B port  │
└────────────────────────┘                           └──────────────────┘                    └──────────────────┘
```

- The **gateway** (this repository) owns the USB port, polls the pump,
  enforces the safety rules and runs the watchdog.
- The **integration**
  ([ha-nibe-service-usb](https://github.com/tumik/ha-nibe-service-usb)) finds
  the gateway, authenticates with the key, creates entities from the sensor
  metadata and sends a heartbeat every 60 s.
- The wire protocol between them is specified in
  [docs/protocol.md](docs/protocol.md).

## Requirements

- A Nibe heat pump with a **service USB port (type B)** on the display unit.
  Tested only on an **F1226-6 R, display firmware 7994R7** (see the warning
  above).
- A USB A-to-B cable that carries data.
- A **Linux** host near the pump, for example a Raspberry Pi running Raspberry
  Pi OS or Debian. The port shows up as a CDC-ACM device, usually
  `/dev/ttyACM0`.
- **Python 3.11 or newer.**
- The user running the gateway must be in the **`dialout`** group to open
  the serial port (the systemd unit handles this for the service user).
- Home Assistant on the same LAN. Discovery needs multicast. Without it, add
  the gateway by hand.

## Install

This runs the gateway from a checkout as your own user so you can try it.
For a permanent install as a systemd service, continue with
[docs/installation.md](docs/installation.md) afterwards.

**1. Install `uv`** (or use plain `pip`, shown below):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**2. Clone and install:**

```bash
git clone https://github.com/tumik/nibe-service-usb.git
cd nibe-service-usb
uv sync
# without uv:  python3 -m venv .venv && .venv/bin/pip install -e .
#              then use .venv/bin/nibe-service-usb instead of "uv run nibe-service-usb"
```

**3. Give your user access to the serial port**, then log out and back in:

```bash
sudo usermod -aG dialout "$USER"
ls -l /dev/ttyACM0 /dev/serial/by-id/      # the pump's port should be listed
```

**4. Configure.** Every key has a default, so the file only needs the keys
you want to change. Usually that is just `pump.port`, if your port is not
`/dev/ttyACM0`.

```bash
cp config.example.toml config.local.toml          # config.local.toml is git-ignored
$EDITOR config.local.toml
export NIBE_SERVICE_USB_CONFIG=$PWD/config.local.toml   # or pass --config config.local.toml to every command
uv run nibe-service-usb check-config              # validates and prints the effective values
```

The server looks for its config in `--config`, then in
`$NIBE_SERVICE_USB_CONFIG`, then in `/etc/nibe-service-usb/config.toml`. If none
of these exist it uses the built-in defaults. See
[docs/configuration.md](docs/configuration.md).

**5. Probe the pump (read-only).** This reads the identity, the clock and a
few registers, then exits. It writes nothing.

```bash
uv run nibe-service-usb probe
```

You should see `online: True`, the product name (e.g. `F1226-6 R`), the
software version and some register values. If you do not, see
[Troubleshooting](#troubleshooting).

**6. Run the gateway:**

```bash
uv run nibe-service-usb run
# with the local debug page:  uv run nibe-service-usb run --debug-website 8477
```

On first start the server creates a random key and a server id in its data
directory (`~/.local/share/nibe-service-usb/` by default, or `--data-dir` /
`$NIBE_SERVICE_USB_DATA_DIR`). It listens on TCP port **9477** and announces
itself over mDNS.

**7. Show the key** (in a second terminal):

```bash
uv run nibe-service-usb show-key
```

This prints the key as 64 hex characters and as dash-grouped base32. Both
forms are the same 32-byte key.

**8. Add the gateway in Home Assistant.** Install
[ha-nibe-service-usb](https://github.com/tumik/ha-nibe-service-usb) as its
README describes, then:

- **Discovery:** Home Assistant shows the gateway as a discovered device under
  *Settings → Devices & services*. Click *Configure* and paste the key.
- **Manual:** *Add integration*, choose the Nibe service USB integration,
  and enter the gateway's **host** (IP or DNS name), **port** `9477` and the
  **key**.

**Without a pump:** `uv run nibe-service-usb simulate --debug-website 0` runs
the whole server against an in-memory pump. It prints the key, the TCP port
and the debug website URL.

## Command line

Options go after the command, e.g. `nibe-service-usb run --config /path/config.toml`.

| Command | What it does |
|---|---|
| `run` | Run the gateway. `--debug-website [HOST:]PORT` enables the debug page. |
| `simulate` | Same server against the in-memory simulator. Discovery is off and the TCP port is random unless the config file sets them. |
| `probe` | Read-only: identity, clock and a few registers, then exit. The exit code is non-zero if the pump does not answer. |
| `check-config` | Validate the config and `sensors.yaml`, then print the effective values. |
| `show-key` | Print the auth key. The server creates the key on its first start; `show-key` never creates one. |
| `rotate-key` | Generate a new key. Restart a running server afterwards, and enter the new key in Home Assistant. |

Common options: `--config PATH`, `--data-dir PATH`, `--log-level
{debug,info,warning,error,critical}`. `nibe-service-usb --version` prints the
version.

## Debug website

The debug website is a single local web page for commissioning and debugging.
It shows a live table of every sensor (value, unit, age), the pump, heating,
hot water and watchdog status, and buttons for the same commands Home
Assistant has. The buttons go through the same allowlist as the TCP API, so
the page cannot do anything Home Assistant cannot.

Enable it on the command line or in the config:

```bash
nibe-service-usb run --debug-website 8477            # http://127.0.0.1:8477 (host from config, default 127.0.0.1)
nibe-service-usb run --debug-website 0               # pick a free port; the URL is logged at startup
```

```toml
[debug_website]
enabled = true
port = 8477
```

> [!CAUTION]
> The debug website has **no authentication**. Anyone who can reach it can
> change hot water and heating settings. Keep it on `127.0.0.1` (the default)
> and use an SSH tunnel from your desktop:
> `ssh -L 8477:127.0.0.1:8477 pi@gateway-host`, then open
> http://127.0.0.1:8477. Bind it to `0.0.0.0` only if you trust everyone on
> the LAN. The server logs a warning when it is bound to a non-loopback
> address.

Commands from the debug page do **not** count as Home Assistant heartbeats
unless `debug_website.feeds_watchdog = true`. Leaving the page open therefore
does not hide a dead Home Assistant from the watchdog.

## Troubleshooting

**`Permission denied` on `/dev/ttyACM0`.** Your user is not in the `dialout`
group. Run `sudo usermod -aG dialout "$USER"`, then log out and back in
(check with `id`). The systemd service gets the group through
`SupplementaryGroups=dialout`.

**The pump does not answer (`pump not reachable`, `online: False`).**
- Check that the device exists (`ls -l /dev/serial/by-id/`) and look at
  `dmesg | grep -i acm` after plugging in the cable. Some USB cables only
  carry power.
- Only one program can use the console at a time. Stop the service
  (`sudo systemctl stop nibe-service-usb`) and close any other serial
  program before running `probe`. On some systems ModemManager probes new
  ACM devices; disable it if it grabs the port.
- The console needs **RTS** asserted. Keep `pump.rts = true` (the default).
- Use `/dev/serial/by-id/...` as `pump.port` if there are several ACM devices.
- Run `nibe-service-usb probe`. For a lower-level look, run
  `scripts/probe_console.py`, which is read-only by default (see
  [docs/development.md](docs/development.md)).

**Home Assistant does not discover the gateway.**
- mDNS needs multicast (UDP 5353) between the two hosts. It does not cross
  VLANs or subnets without an mDNS reflector, and it does not reach Home
  Assistant in a container without host networking.
- Check that `discovery.enabled` is `true` and that no firewall on the
  gateway blocks UDP 5353 or TCP 9477.
- Use **manual setup** with the gateway's IP address and port 9477. Give the
  gateway a **DHCP reservation** so its address does not change.

**`No heartbeat for 300 s: reverting to automatic control` in the log.** Home
Assistant stopped pinging the gateway. Possible reasons: the integration is
not set up, it uses an old key (after `rotate-key`), Home Assistant was down
for more than `watchdog.timeout`, or the network between them failed. The
heating request is now `auto` and the hot water mode is
`defaults.hot_water_mode`. The integration sends its desired state again
when it reconnects. A server that never received a heartbeat does not revert
anything.

**Heating request `active: false`.** `get_status.heating.suspended_reason`
says why. Examples: `dm_zero` (the pump is holding the degree minutes at 0
because heating is blocked), `safety_supply_temp`, `stale_data`,
`dm_address_invalid` (the RAM address check failed; see the warning above).
[docs/heating-control.md](docs/heating-control.md) lists all reasons.

## Documentation

| Document | Content |
|---|---|
| [docs/installation.md](docs/installation.md) | Permanent install as a systemd service, logs, upgrades, key handling |
| [docs/configuration.md](docs/configuration.md) | Config file reference, data directory, CLI |
| [docs/development.md](docs/development.md) | Dev setup, tests, simulator, console probe script, project layout |
| [docs/protocol.md](docs/protocol.md) | TCP wire protocol between Home Assistant and the gateway (the integration's contract) |
| [docs/discovery.md](docs/discovery.md) | mDNS auto-discovery and manual setup |
| [docs/security.md](docs/security.md) | Threat model, key, authentication, watchdog |
| [docs/heating-control.md](docs/heating-control.md) | Degree-minute heating enable/disable logic and safety guards |
| [docs/sensors-and-polling.md](docs/sensors-and-polling.md) | Sensor definitions, polling cadence, change detection |
| [docs/sensor-selection.md](docs/sensor-selection.md) | Which pump values are exposed, and in which tier |
| [docs/pump-interface.md](docs/pump-interface.md) | Service USB console: serial settings, commands, writes, health checks |
| [docs/architecture.md](docs/architecture.md) | Process layout, modules, concurrency, data flow |

Release notes are in [CHANGELOG.md](CHANGELOG.md).

## Non-goals

- Not a transparent proxy for the Nibe service protocol.
- Not a Home Assistant custom component. That is
  [ha-nibe-service-usb](https://github.com/tumik/ha-nibe-service-usb).
- Not a cloud service. It works on the LAN only.

## To do

- Feature to force water heating with max power. If running out of water, force both compressor and additional heating to heat water.

## License

[MIT](LICENSE). This project is not affiliated with or endorsed by NIBE.
