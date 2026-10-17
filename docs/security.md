# Security and watchdog

## Threat model

* The server sits on a home LAN. Anyone on that LAN (compromised IoT device,
  guest Wi-Fi misconfiguration) can reach the TCP port and the serial device
  behind it controls a heat pump.
* Goals, in order: (1) no one without the key can change anything on the pump;
  (2) even with the key, only the fixed allowlist of operations is possible,
  each with bounded parameters; (3) a dead or misbehaving Home Assistant can
  never leave the pump in a bad state.
* Out of scope: an attacker with root on the gateway host, physical access to
  the USB cable.

## Key

* Generated at first start (or by the first `show-key`):
  `secrets.token_bytes(32)`. Stored in `<data_dir>/auth.key` as lowercase
  hex, written atomically with file mode `0600`; a data directory created by
  the server gets mode `0700`. Looser permissions on an existing key file are
  fixed (with a warning) when it is read.
* The key is **never written to the log**. At generation the log only says
  ``Generated a new auth key; run `nibe-service-usb show-key` to display it``.
* `nibe-service-usb show-key` prints it any time; it only works for a user
  that can read the file (the service user or root — run it as the service
  user, see `installation.md`). The user copies it into the HA config flow.
* `nibe-service-usb rotate-key` writes a new key. A running server is not
  notified and keeps using the old key until it is restarted; the restart
  drops all connections, and HA must then be given the new key.
* Format shown to the user: 64 hex characters, and the same 32 bytes as
  dash-grouped base32 (groups of 8) for easier reading and typing.

## Authentication: HMAC challenge/response

Chosen over sending the key in clear and over TLS:

* The key never crosses the wire. A passive sniffer only sees nonces and HMACs.
* Mutual: the client also verifies the server, so an mDNS-spoofing device
  cannot harvest commands or feed fake sensor data to HA.
* Replay-safe: the server nonce is fresh per connection.
* No certificates to generate, pin, distribute or renew on the HA side.

Limitation accepted: after the handshake the stream is plaintext, so a LAN
attacker with active MITM capability could inject commands into an established
session. Given the command set (no command can damage the pump or disable
safety logic; worst case is hot water set to eco or heating request toggled,
both of which the pump overrides within its own limits) this is acceptable for
protocol version 1. **Possible future option** (not implemented): wrap the connection in TLS using a self-signed cert
generated at first start, with the cert fingerprint published in the `hello`
and TXT record and pinned by HA. The handshake stays the same inside TLS.
`Python ≥ 3.13` also offers TLS-PSK which would reuse the key directly;
revisit once HA's minimum Python makes that viable.

### Handshake hardening

* Handshake deadline 10 s; one `auth` attempt per connection.
* Constant-time comparison (`hmac.compare_digest`).
* Per-source-IP failure counter: after 5 failures within 10 minutes, that IP
  gets `auth_failed` immediately with a 5 s delay for the next 10 minutes.
  Every failure is logged with the source address.
* Max 4 authenticated connections, max 8 total sockets; beyond that new
  connections are closed immediately. Unauthenticated sockets are reaped at
  the deadline so a connection flood cannot block HA for long.

## Command allowlist

The dispatcher is a static dict `cmd name → (handler, param schema)`. There is
no generic "read register" / "write register" command and the API layer has no
import of the register map. Every parameter is validated against a closed
schema (enums, no free-form strings reaching the pump layer). Adding a new
operation is a deliberate code change, not a configuration knob.

The core layer additionally enforces value bounds on anything it writes
(hot water mode ∈ {0,1,2}, periodic increase bit ∈ {0,1}, degree minutes ∈
[configured min, 0]) and the transport allowlists the exact registers for
`WI` and the exact address and length for `WM`, so that even a bug in the API
layer cannot produce an out-of-range or off-target write.

## Transport hardening

* Max line 64 KiB; max 3 malformed lines per connection.
* Idle timeout: if an authenticated connection sends nothing for 10 minutes it
  is closed (HA pings every minute; a silent socket is a dead socket).
* TCP keepalive enabled on accepted sockets.
* `listen_host` configurable to bind to a single interface.

## Watchdog ("HA liveness")

* HA sends `ping` every 60 s. A successful `auth` and every authenticated
  request update `last_heartbeat`. Commands from the debug website do not,
  unless `debug_website.feeds_watchdog = true`.
* `ha_alive` starts as `false`. The watchdog only arms after the first
  heartbeat: a server that has never heard from HA never reverts anything
  (it would otherwise write the default hot water mode onto a pump nobody
  asked it to touch).
* A background task checks every 10 s. If `now - last_heartbeat > 300 s`
  (configurable) and `ha_alive` is still true:
  1. `ha_alive = False`, log a warning
     (`No heartbeat for 300 s: reverting to automatic control`).
  2. Heating request → `auto` (stop writing degree minutes). Nothing is written
     to the pump; it simply resumes its own control.
  3. Hot water mode → `defaults.hot_water_mode` if set in config (e.g.
     `"normal"`); if unset, leave as is. Written with read-back like a normal
     command.
  4. `status` event to any remaining connections, `reverted_at` recorded.
* A connection drop does **not** trigger the revert by itself; only the 5-minute
  silence does. This lets HA restart or update without the pump state
  flapping.
* When the next heartbeat arrives, `ha_alive = True` again, but the modes are
  **not** restored automatically. HA re-sends its desired state (the HA
  integration is expected to do this on every (re)connect), which is the authoritative
  path. This keeps the server stateless about HA's intent across outages.
* Server restart: modes start as `auto`, same reasoning.

## Safety guards for heating control

Documented in `heating-control.md`; summarised here because they are part of
the security story: the server refuses to push "heating disabled" while any
configured temperature guard is violated, and it never writes a degree-minute
value outside the configured bounds.

## Debug website

`--debug-website` serves an unauthenticated control page. Mitigations: off by
default, binds `127.0.0.1` unless a host is given explicitly, uses the same
command allowlist as the TCP API (so it cannot do anything HA cannot), logs
every command with the source address, and prints a warning at startup when
bound to a non-loopback address. For remote use, prefer an SSH tunnel to the
localhost port over exposing it.

## Logging and auditing

Every accepted command is logged with source address, client string, command
and parameters at `INFO`. Auth failures, watchdog trips and safety suspensions
at `WARNING`. No sensor values at `INFO` (too chatty); `DEBUG` has everything
including raw frames (with the key never logged anywhere).
