# Development

## Setup

The project uses [uv](https://docs.astral.sh/uv/), a `src/` layout and
Python 3.11+.

```bash
git clone https://github.com/tumik/nibe-service-usb.git
cd nibe-service-usb
uv sync --locked          # runtime + dev dependencies (pytest, pytest-asyncio, ruff) from uv.lock
uv run nibe-service-usb --help
```

Runtime dependencies are deliberately few: `pyserial-asyncio-fast`,
`zeroconf` and `pyyaml`. Everything else (TCP server, HMAC, TOML, the debug
website's HTTP server) uses the standard library.

## Tests and linting

```bash
uv run pytest -q                          # whole suite, no hardware needed
uv run pytest tests/test_core_heating.py  # one file
uv run ruff check src tests               # lint (rules E, F, I, B, UP, ASYNC; line length 110)
uv run ruff check --fix src tests
```

`pytest` runs with `asyncio_mode = "auto"`, so `async def test_...` works
without decorators. The tests use the pump simulator and fake clocks. The
end-to-end tests start a real TCP server on a random port.

**CI:** [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on
every push to `main` and on every pull request, with Python 3.11, 3.12 and
3.13: `uv sync --locked`, `uv run ruff check src tests`, `uv run pytest -q`.
Run the same three commands locally before pushing. If you change
dependencies in `pyproject.toml`, run `uv lock` and commit `uv.lock`.

## Running without a pump: `simulate`

```bash
uv run nibe-service-usb simulate --debug-website 0
```

This starts the complete server (poller, controllers, watchdog, TCP API, debug
website) against `SimulatedPumpClient` (`src/nibe_service_usb/pump/simulator.py`)
instead of the serial port. It prints the key, the TCP port and the debug
website URL. Differences from `run`:

- mDNS discovery is **off** and the TCP port is **random**, unless your
  config file sets `discovery.enabled` or `server.listen_port`. Set
  `server.listen_port = 9477` and `discovery.enabled = true` in a config
  file to test discovery from a real Home Assistant.
- The key and server id come from the normal data directory
  (`~/.local/share/nibe-service-usb/` or `--data-dir`). Use a separate
  `--data-dir` if you do not want to share them with a real gateway.

The simulator holds an in-memory register table seeded with values like
those of the test unit. It models degree minutes (integrated once per
simulated minute when heat demand is 1, reset to 0 when it is 0) and the
compressor state machine around the start threshold. It counts EEPROM store
calls for `WI` but not for `WM`, and it emulates the periodic-increase timer.
Tests can also inject latency and failures, `unplug()`/`plug()` it, and
`advance()` its clock.

### Talking to the API by hand

The protocol is JSON lines over TCP ([protocol.md](protocol.md)), but the
handshake needs an HMAC, so plain `nc` is not enough. A minimal client using
the package's own helpers:

```python
# uv run python client.py 127.0.0.1 PORT KEY
import asyncio, base64, json, secrets, sys

from nibe_service_usb.api.auth import compute_proof, verify_server_proof
from nibe_service_usb.keystore import parse_key


async def main(host: str, port: int, key_text: str) -> None:
    key = parse_key(key_text)  # hex or dash-grouped base32
    reader, writer = await asyncio.open_connection(host, port)

    async def request(id_: int, cmd: str, params: dict | None = None) -> dict:
        writer.write((json.dumps({"id": id_, "cmd": cmd, "params": params or {}}) + "\n").encode())
        while True:
            msg = json.loads(await reader.readline())
            if msg.get("id") == id_:
                return msg

    hello = json.loads(await reader.readline())
    server_nonce = hello["data"]["nonce"]
    client_nonce = base64.b64encode(secrets.token_bytes(32)).decode()
    result = await request(1, "auth", {
        "proto": 1, "client": "example/0.1", "client_nonce": client_nonce,
        "proof": compute_proof(key, server_nonce, client_nonce),
    })
    assert result["ok"], result
    assert verify_server_proof(key, server_nonce, client_nonce, result["data"]["server_proof"])
    print(json.dumps((await request(2, "get_status"))["data"], indent=2))
    writer.close()


asyncio.run(main(sys.argv[1], int(sys.argv[2]), sys.argv[3]))
```

For clicking around, the debug website is easier.

## Console probe script: `scripts/probe_console.py`

A standalone script (it uses only `pyserial`, declared inline per PEP 723)
that talks to the service console directly. It sends one command at a time
and prints the raw reply bytes together with an interpretation. It was used
to measure everything in [pump-interface.md](pump-interface.md). Use it to
check a new unit or firmware before you trust the server with it.

Stop the gateway first, because only one program can use the port.

```bash
uv run scripts/probe_console.py --dry-run            # print what would be sent; opens nothing
uv run scripts/probe_console.py                      # default: read-only set
uv run scripts/probe_console.py --log probe.log      # also append a raw transcript
```

| Mode | Sends | Writes to the pump? |
|---|---|---|
| *(default)* | `RV`, `b`, `QR`, `AI`, `RT 1` and a series of `RI` reads (temperatures, heating, hot water, compressor, identity, EEPROM health, one unknown id) | **No** |
| `--write-test` | Default set **plus** `RI 40940 40969`, `WI 40940 -20`, the same read again, and a read 70 s later | **Yes**: one degree-minute register write (raw -20 = -2.0 DM), which costs one EEPROM store call |
| `--limits` | `RI` lines of 6 to 11 registers and lines padded to 58 to 62 characters, each followed by an `RV` sync | No |
| `--eeprom` | Reads the EEPROM store counter for 3 minutes, then `WI 40940 -20/-21/-22` and reads again | **Yes**: three register writes (three EEPROM store calls) |
| `--memwrite` | `WM` RAM writes of -20, -256 and +256 to the degree-minute RAM address `0x2038af18`, read-back via `RI` and `RM`, then reads the periodic-increase timer | **Yes**: RAM writes (no EEPROM) |

Other options: `--port` (default `/dev/ttyACM0`), `--only LABEL ...` (run a
subset by label, as printed in the command list), `--delay`, `--idle`,
`--max-wait`, `--no-rts`, `--no-dtr`, `--baud`.

> [!WARNING]
> `--write-test`, `--eeprom` and `--memwrite` write to the pump, and their
> register numbers and RAM addresses were found on an F1226-6 R with
> firmware 7994R7. On any other unit, run the default (read-only) mode and
> `--limits` only, and compare the results with
> [pump-interface.md](pump-interface.md) before writing anything.

The built-in `nibe-service-usb probe` command is a shorter read-only check
that uses the server's own transport (identity, clock and ten registers).

## Project layout

```
nibe-service-usb/
├── pyproject.toml, uv.lock
├── config.example.toml          # every key with its default
├── systemd/nibe-service-usb.service
├── scripts/probe_console.py     # standalone console probe (see above)
├── docs/
├── tests/                       # pytest, one file per module or feature
└── src/nibe_service_usb/
    ├── cli.py                   # run | simulate | probe | check-config | show-key | rotate-key
    ├── config.py                # dataclasses, TOML loading, validation, data dir
    ├── keystore.py              # auth.key / server_id, key formats
    ├── app.py                   # wires components, start/stop order, `probe`
    ├── sensors.yaml             # packaged sensor and alarm definitions
    ├── api/                     # TCP server, JSON-lines framing, HMAC auth, command allowlist, sessions
    ├── core/                    # state + event bus, poller, heating, hot water, watchdog, safety guards
    ├── pump/                    # serial transport, codec (verbs, allowlists), client queue, registers, simulator
    ├── discovery/mdns.py        # zeroconf advertisement with address watcher
    └── debug/                   # debug website (web.py + index.html)
```

The layering is strict. `api/` never names a register, `core/` decides what
is read and written, and `pump/` knows nothing about Home Assistant. See
[architecture.md](architecture.md).

## Design documents

| Document | Role |
|---|---|
| [protocol.md](protocol.md) | **Contract** with the Home Assistant integration. Change it only with the versioning rules at its end. |
| [architecture.md](architecture.md) | Components, concurrency model, data flow |
| [pump-interface.md](pump-interface.md) | Console commands, write rules, health checks, console behaviour |
| [heating-control.md](heating-control.md) | Degree-minute controller decision table and guards |
| [sensors-and-polling.md](sensors-and-polling.md), [sensor-selection.md](sensor-selection.md) | What is read, when, and how it is published |
| [security.md](security.md), [discovery.md](discovery.md) | Auth, threat model, watchdog; mDNS |
| [configuration.md](configuration.md) | Config keys, data directory, CLI |

## Conventions

- Type hints everywhere, `from __future__ import annotations`. Frozen
  dataclasses for records. Plain dicts only on the wire and in snapshots.
- `logging.getLogger(__name__)`. No `print` outside `cli.py`. Never log the key.
- Timestamps that leave the process are `time.time()` floats. Intervals use
  `time.monotonic()`.
- Timeouts, intervals and addresses come from `Config`. Do not hard-code
  values that [configuration.md](configuration.md) lists.
- Console limits: never send a line longer than 61 characters or an `RI` with
  more than `pump.ri_batch_size` registers, and never send a verb outside the
  transport allowlist. Writes are limited to `WRITABLE_REGISTERS` /
  `WRITABLE_RAM` in `pump/interface.py`.
- Sensor `name`s in `sensors.yaml` are wire identifiers (Home Assistant
  entity ids depend on them). Add new sensors freely, but never rename
  existing ones. Validate with `nibe-service-usb check-config`.
- A new API command needs an entry in `api/commands.py`, an update of
  [protocol.md](protocol.md), and an update of the integration.
