# Architecture

## Overview

One Python process, asyncio based, running as a systemd service on the host
that owns the USB service cable. Three layers, strictly separated:

```
                 TCP (JSON lines)                    serial / USB
Home Assistant  <----------------->  nibe-service-usb  <------------>  Heat pump
                                     ┌───────────────┐
                                     │  api/         │  TCP server, auth, command allowlist,
                                     │               │  subscriptions, event push
                                     ├───────────────┤
                                     │  core/        │  state, sensor poller, heating controller,
                                     │               │  hot water controller, watchdog
                                     ├───────────────┤
                                     │  pump/        │  serial transport, frame codec,
                                     │               │  register map, request queue
                                     └───────────────┘
                                     discovery/ (mDNS)   config/ (TOML + key store)   cli/
```

* `api/` only knows about high-level commands and the `core` services.
  It never touches a register number.
* `core/` owns all behaviour and state. It is the only layer that decides
  *what* to read or write on the pump and *when*.
* `pump/` knows how to talk to the pump and nothing about Home Assistant.
  It exposes register reads (`read`, `read_many`, `read_temperatures`), RAM
  reads (`read_ram`), the two allowlisted write paths (`write` = `WI`,
  `write_ram` = `WM`), identity/clock queries and the connection state
  (see `pump-interface.md`).

The strict split is what makes the security promise hold: the TCP layer has no
code path that can forward an arbitrary register operation.

## Package layout

```
nibe-service-usb/
├── pyproject.toml
├── src/nibe_service_usb/
│   ├── __main__.py            # `python -m nibe_service_usb` -> cli
│   ├── cli.py                 # run | simulate | probe | check-config | show-key | rotate-key
│   ├── config.py              # TOML loading, defaults, validation, data dir
│   ├── keystore.py            # key generation / storage / rotation
│   ├── app.py                 # wires everything together, lifecycle, shutdown, `probe`
│   ├── sensors.yaml           # packaged sensor + alarm definitions (name, register, scale, unit, …)
│   ├── api/
│   │   ├── server.py          # asyncio TCP server, connection lifecycle
│   │   ├── framing.py         # JSON-lines read/write, size limits
│   │   ├── auth.py            # challenge/response HMAC handshake, rate limiting
│   │   ├── commands.py        # allowlist: command name -> handler + param schema
│   │   └── session.py         # per-connection state (authenticated, subscriptions)
│   ├── core/
│   │   ├── state.py           # single source of truth, publishes change events
│   │   ├── events.py          # tiny in-process pub/sub used by api and core
│   │   ├── poller.py          # periodic sensor polling + change detection
│   │   ├── heating.py         # degree-minute controller (enable/disable/auto)
│   │   ├── hot_water.py       # comfort mode + periodic increase trigger
│   │   ├── watchdog.py        # HA heartbeat tracking + revert-to-defaults
│   │   ├── safety.py          # temperature guard rules used by heating.py
│   │   └── errors.py          # CommandError (wire error codes)
│   ├── pump/
│   │   ├── interface.py       # PumpClient protocol, Priority, errors, write allowlists
│   │   ├── client.py          # SerialPumpClient: connect/reconnect, priority queue, timeouts, resync
│   │   ├── transport.py       # pyserial-asyncio-fast wrapper: one CR LF terminated line out, reply in
│   │   ├── codec.py           # command encoding (verb + write allowlist), reply parsing
│   │   ├── registers.py       # sensors.yaml loading, typed value decoding, wire metadata
│   │   └── simulator.py       # in-memory pump for `simulate` and tests
│   ├── discovery/
│   │   └── mdns.py            # zeroconf service registration
│   └── debug/
│       ├── web.py             # --debug-website: tiny HTTP + SSE server on the event loop
│       └── index.html         # single page: live sensor table + command buttons
├── config.example.toml
├── systemd/nibe-service-usb.service
├── scripts/probe_console.py   # standalone console probe
├── tests/
└── docs/
```

## Runtime components

### PumpClient (`pump/client.py`)

* Owns the serial port. Exactly one outstanding request at a time: all reads
  and writes go through a single `asyncio.PriorityQueue`.
* Priorities: `COMMAND` (user-triggered writes and their read-back) >
  `CONTROL` (degree-minute pushes, safety reads) > `POLL` (scheduled sensor
  polling). A slow 10-minute poll must never delay a hot water command.
* Per-request timeout; N retries; after `pump.offline_after` seconds without a
  reply it marks the pump `offline`, emits a status event, and runs a
  reconnect loop (first retry after 5 s, then doubling up to 30 s). The
  serial device may disappear and reappear (USB re-enumeration), so
  reconnect re-opens by path.
* The console does not send unsolicited data in normal operation. The
  transport discards stale input when it connects and during every resync,
  and every reply is validated (token count, expected format). A mismatch
  triggers an `RV` resync (see `pump-interface.md`).

### State (`core/state.py`)

Single in-memory object holding:

* pump connection status
* last known value + timestamp for every sensor
* heating request mode (`auto` / `enable` / `disable`), whether the controller
  is currently active or suspended by a safety guard, time of last push
* hot water comfort mode as last read from the pump
* watchdog status (last heartbeat time, `ha_alive` boolean)

Every change publishes on the internal event bus. The API layer subscribes and
converts to wire events. Nothing in the API layer keeps its own copy of pump
state; on `subscribe` it reads the snapshot from here.

### SensorPoller (`core/poller.py`)

See `sensors-and-polling.md`. A fast cycle every minute (`poll.fast_interval`)
and a full cycle every 10 minutes (`poll.interval`); diffs against `State`
and publishes changes. Also runs the pump health check at (re)connect and
daily.

### HeatingController (`core/heating.py`)

See `heating-control.md`. Tri-state controller (`auto` / `enable` /
`disable`) that evaluates every 10 minutes while a request is active and
writes the degree-minute RAM copy when that moves DM towards the request and
the guards allow it.

### HotWaterController (`core/hot_water.py`)

Translates `eco/normal/lux` to the register value (R47041), writes it, reads it
back, publishes the resulting value. Triggers the periodic increase
(anti-legionella) by toggling its enable bit (R47050) and confirms the start
from the run timer in RAM (see `pump-interface.md`).

### Watchdog (`core/watchdog.py`)

See `security.md`. Tracks heartbeats, reverts to defaults after 5 minutes of
silence. It never reverts before the first heartbeat has been seen.

### API server (`api/`)

See `protocol.md`. Accepts TCP connections, runs the auth handshake, then
dispatches allowlisted commands to `core`. Pushes events to subscribed
sessions.

### Discovery (`discovery/mdns.py`)

See `discovery.md`. Registers the service with zeroconf while the server is
listening; unregisters on shutdown.

### Debug website (`debug/web.py`, `--debug-website`)

A development and commissioning aid, off by default.

* Started by `run --debug-website [HOST:]PORT` (or `[debug_website]` in
  config). `PORT` 0 picks a free port; the URL is logged at startup.
  Binds `127.0.0.1` unless a host is given.
* Implemented on the same asyncio loop with stdlib only (`asyncio.start_server`
  + hand-written HTTP/1.1 for four routes). No web framework, no JS build:
  one HTML page shipped in the package.
  * `GET /` — the page: a table of every sensor (name, value, unit, age),
    status panel (pump online, heating mode/active/reason, hot water mode,
    periodic increase running/remaining/hold, watchdog, identity, EEPROM
    counters, uptime) and buttons.
  * `GET /state` — JSON snapshot used for the initial page load.
  * `GET /events` — Server-Sent Events stream: the page subscribes once and
    gets the same `sensor_update` / `status` payloads the TCP clients get,
    so the table updates in real time without polling.
  * `POST /cmd` — JSON `{cmd, params}` that goes through **the same command
    dispatcher and allowlist** as the TCP API (`api/commands.py`), so the
    buttons can do exactly what HA can do and nothing more: hot water mode
    eco/normal/lux, trigger periodic increase, heating request
    auto/enable/disable, refresh. The page shows the result or error code.
* The debug session is treated as an authenticated client for the watchdog
  only while the page is open **and** `debug_website.feeds_watchdog = true`
  (default false), so leaving the page open does not mask a dead HA.
* No authentication. That is why it binds to localhost by default; exposing
  it on the LAN is a conscious choice (see `security.md`).

## Concurrency model

* Single asyncio event loop, no threads except what `pyserial-asyncio` and
  `zeroconf` use internally.
* Long-running tasks (poller, heating controller, watchdog, pump reconnect
  loop, mDNS) are created in `app.py` and cancelled in reverse order on
  shutdown. `SIGTERM` triggers a graceful shutdown: stop accepting connections,
  send a `shutdown` event to clients, unregister mDNS, close serial.
* Shutdown deliberately does **not** write anything to the pump. Leaving the
  pump alone is the safe state; it recovers by itself.

## Data flow examples

**Sensor update**

```
timer → Poller.poll_all() → PumpClient.read(reg) × N → State.set(sensor, value)
      → event bus "sensor_changed" → api.Session.on_event → write `sensor_update` line
```

**Set hot water mode**

```
HA line {"id":7,"cmd":"set_hot_water_mode","params":{"mode":"lux"}}
 → api.commands.dispatch (validate params against schema)
 → HotWaterController.set_mode("lux")
 → PumpClient.write(reg, 2) (priority COMMAND) → PumpClient.read(reg) → State.set(...)
 → result {"id":7,"ok":true,"data":{"mode":"lux"}}
 → (separately) `sensor_update` event to all subscribers because State changed
```

**Watchdog trip**

```
no ping for 300 s → Watchdog.on_timeout()
 → HeatingController.set_mode("auto")   (stop touching degree minutes)
 → HotWaterController.set_mode(config.defaults.hot_water_mode) if configured
 → State.watchdog.ha_alive = False → `status` event to any still-connected client
```

## Deployment

* Target: Raspberry Pi (64-bit Raspberry Pi OS / Debian) or any Linux host,
  Python ≥ 3.11 (`tomllib` in stdlib). CI tests 3.11, 3.12 and 3.13.
* Dependencies kept minimal: `pyserial-asyncio-fast`, `zeroconf`, `pyyaml`
  (for `sensors.yaml`). No web framework, even for the debug website. Stdlib
  `json`, `hmac`, `secrets`, `tomllib`, `asyncio`.
* Installed as a `uv`/`pip` project into a venv under `/opt/nibe-service-usb`;
  `systemd/nibe-service-usb.service` runs it as a dedicated unprivileged user
  with the `dialout` group, `ProtectSystem=strict` and the data directory as
  its only writable path (`StateDirectory=`). See `installation.md`.
* Logging to stderr (journald under systemd) as
  `time LEVEL logger: message` lines; `--log-level` flag and `log.level`
  config option.

## Testing strategy

* `pump/simulator.py` implements the same interface as `PumpClient` with an
  in-memory register table and configurable latency/failure injection. Used by
  `nibe-service-usb simulate` to run the full server without hardware, and by
  tests.
* Protocol tests open a real TCP connection against the server with the
  simulator behind it: handshake, auth failure, allowlist enforcement, event
  delivery, watchdog trip (with a faked clock).
* Heating controller unit tests cover the decision table in
  `heating-control.md`.
