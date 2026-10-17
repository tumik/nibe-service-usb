# Wire protocol (Home Assistant ⇄ server)

## Transport

* TCP, default port **9477** (configurable). Server binds to `0.0.0.0` by
  default; can be restricted to one interface.
* One persistent connection per client. Home Assistant keeps it open and
  reconnects with backoff when it drops.
* **JSON Lines**: every message is one UTF-8 JSON object terminated by `\n`.
  No other framing. Maximum line length 64 KiB; a longer line closes the
  connection.
* Protocol version `proto = 1`. Carried in `hello` (server) and `auth`
  (client). Server rejects a client whose `proto` it does not support.

Rationale: JSON lines are trivial to implement on both sides with asyncio
streams, easy to debug with `nc`, and the message rate is tiny (a few lines per
minute). No need for a binary format or a web framework.

## Message shapes

Three kinds, distinguished by which top-level keys are present:

| Kind | Direction | Shape |
|---|---|---|
| Request | client → server | `{"id": <int>, "cmd": "<name>", "params": {...}}` |
| Result | server → client | `{"id": <int>, "ok": true, "data": {...}}` or `{"id": <int>, "ok": false, "error": {"code": "...", "message": "..."}}` |
| Event | server → client | `{"event": "<name>", "ts": <unix float>, "data": {...}}` |

* `id` is chosen by the client, must be unique among in-flight requests on the
  connection. Server echoes it. Requests may be pipelined; results may come
  back out of order (a pump write takes longer than a `ping`).
* `params` may be omitted when empty.
* Unknown top-level keys are ignored (forward compatibility). Unknown `cmd`
  → error `unknown_command`. Malformed JSON → error with `id: null`, then after
  3 malformed lines the connection is closed.

## Connection lifecycle

```
client                                   server
  |  --- TCP connect ------------------>   |
  |  <-- event hello -------------------   |   nonce, server_id, proto, version
  |  --- req auth ---------------------->  |   HMAC over nonces
  |  <-- result (ok / auth_failed) ------  |   mutual proof on success
  |  --- req subscribe ----------------->  |
  |  <-- result {sensors snapshot} ------  |
  |  --- req ping (every 60 s) --------->  |
  |  <-- result pong --------------------  |
  |  <-- event sensor_update ------------  |   whenever a value changes
  |  --- req set_hot_water_mode -------->  |
  |  <-- result ------------------------  |
  |  <-- event status -------------------  |   mode/pump/watchdog changes
```

* Until `auth` succeeds, the only accepted command is `auth`. Anything else →
  `auth_required` and the connection is closed. The handshake must complete
  within 10 s or the connection is closed.
* After `auth`, all commands below are available. There is no privilege
  hierarchy; one key, one role.
* Several simultaneous authenticated connections are allowed (limit 4) so a
  debugging client can coexist with Home Assistant. Heating / hot water state is
  global server state, not per connection; the last command wins.

## `hello` event (server → client, immediately after connect)

```json
{"event":"hello","ts":1760000000.0,"data":{
  "server":"nibe-service-usb","version":"0.1.0","proto":1,
  "server_id":"6f1c…","nonce":"<base64, 32 random bytes>",
  "auth":"hmac-sha256"}}
```

`server_id` is a UUID generated once at first start and also published in the
mDNS TXT record; Home Assistant uses it as the stable unique id of the device.

## Commands

### `auth`

```json
{"id":1,"cmd":"auth","params":{
  "proto":1,
  "client":"home-assistant/2026.10",
  "client_nonce":"<base64, 32 random bytes>",
  "proof":"<hex HMAC-SHA256(key, server_nonce || client_nonce)>"}}
```

Result on success:

```json
{"id":1,"ok":true,"data":{"server_proof":"<hex HMAC-SHA256(key, client_nonce || server_nonce)>"}}
```

The client verifies `server_proof` so a rogue device on the LAN announcing the
same mDNS name cannot impersonate the real server. `key` is the raw 32-byte key
(see `security.md`); nonces are concatenated as raw bytes in the stated order.
On failure: `{"ok":false,"error":{"code":"auth_failed"}}` and the connection
is closed after a 1 s delay.

### `ping`

Heartbeat. Home Assistant sends one every **60 s**. No params.

```json
{"id":2,"ok":true,"data":{"uptime":12345.6,"pump_online":true,"ha_alive":true}}
```

Any authenticated request also counts as a heartbeat, but HA should still send
`ping` on a timer so the watchdog is fed during idle periods.

### `subscribe`

Subscribes this connection to sensor updates and returns the current snapshot
plus sensor metadata (so HA can create entities without a separate call).

```json
{"id":3,"cmd":"subscribe","params":{"sensors":["*"]}}
```

`sensors` is `["*"]` or an explicit list of sensor names. Result:

```json
{"id":3,"ok":true,"data":{
  "poll_interval":600,
  "sensors":{
    "bt1_outdoor":{"value":-3.4,"unit":"°C","kind":"temperature","ts":1760000000.0,
                    "name":"Outdoor temperature (BT1)"},
    "degree_minutes":{"value":-120,"unit":"DM","kind":"number","ts":…},
    "hw_comfort":{"value":"normal","kind":"enum","options":["eco","normal","lux"],"ts":…},
    "compressor_running":{"value":true,"kind":"binary","ts":…}
  }}}
```

If the snapshot is older than one poll interval or the pump has never been
read, the server first triggers a poll (priority `POLL`) and then answers, so
the first values are fresh. If the pump is offline the snapshot is returned
anyway (last known values; `value: null` for sensors never read) and
`pump.online` is `false` in status; values arrive later as events.

Metadata conventions: each sensor carries `name` (label), `kind`, `unit`,
`precision`, `tier`, `device_class`, `state_class` and `description`; unset
fields are `null` (never empty strings); `precision` is non-null only for
numeric kinds (`temperature`, `number`, `duration`); `options` is present only
for `enum`; `poll_interval` is an integer number
of seconds. `identity` in `get_status` comes from the health check
(`pump-interface.md`); `clock_drift` is `null` while `clock_state` is
`"unset"`.

Re-subscribing replaces the previous subscription. `unsubscribe` (no params)
stops events.

### `list_sensors`

Same metadata as `subscribe` but without values and without subscribing.
Useful for the HA config flow preview. No params.

### `get_status`

Full server state for diagnostics and HA diagnostic entities. No params.

```json
{"id":4,"ok":true,"data":{
  "version":"0.1.0","uptime":…,
  "pump":{"online":true,"port":"/dev/ttyACM0","last_ok":…,"errors":0},
  "heating":{"mode":"disable","active":true,"suspended_reason":null,
             "last_push":…,"next_push":…,"target_dm":-1,"current_dm":-15},
  "hot_water":{"mode":"normal","effective_mode":"normal","reason":"comfort",
               "periodic_increase":{"running":false,"remaining_seconds":0,"hold":false,"enabled":true}},
  "watchdog":{"ha_alive":true,"last_heartbeat":…,"timeout":300,
              "reverted_at":null},
  "identity":{"product":"F1226-6 R","software_version":"7994R7","base_version":25,
              "product_code":"06522820001001","clock_state":"unset","clock_drift":null,
              "uptime":91602,"last_restart":null,"eeprom_store_calls":96860,
              "eeprom_write_failures":0,"eeprom_write_failed":false,"last_health_check":…},
  "clients":2}}
```

### `set_hot_water_mode`

```json
{"id":5,"cmd":"set_hot_water_mode","params":{"mode":"lux"}}
```

`mode` ∈ `eco | normal | lux`. Server writes the register, reads it back,
returns `{"mode":"lux"}` as actually read from the pump. Errors:
`invalid_params`, `pump_offline`, `pump_timeout`, `write_failed`.

### `trigger_hot_water_boost`

Starts the periodic hot water increase (anti-legionella cycle) **now** by
toggling the pump's periodic-increase enable bit (R47050: 0, wait 5 s, 1) and
verifying that the run timer started. No params. Takes about 5–20 s.

```json
{"id":5,"ok":true,"data":{"started":true,"remaining_seconds":18000,"enabled_periodic_increase":false}}
```

`enabled_periodic_increase: true` means the bit was 0 before (periodic
increase was switched off on the panel) and is now left at 1. Errors:
`already_running`, `rate_limited` (one trigger per 10 min), `trigger_failed`
(timer did not start; the flags read are in `message`), `pump_offline`,
`write_failed`. Progress afterwards is visible in the sensors `pi_running`,
`pi_remaining`, `pi_hold` (polled every 60 s while running) and in
`get_status.hot_water`.

### `set_heating_request`

```json
{"id":6,"cmd":"set_heating_request","params":{"mode":"disable"}}
```

`mode` ∈ `auto | enable | disable`.

* `auto`: server stops touching degree minutes. Pump runs its own logic.
* `enable` / `disable`: see `heating-control.md`. The mode persists until
  changed, until the watchdog trips, or until the server restarts (modes are
  **not** persisted across restarts; HA is expected to re-send its desired
  state after reconnecting, which it will do on every reconnect).

The first degree-minute push happens immediately as part of this command when
guards allow it; the result reports what happened:

```json
{"id":6,"ok":true,"data":{"mode":"disable","active":true,"suspended_reason":null,
                           "current_dm":-15,"written_dm":-1}}
```

`active: false` with `suspended_reason: "safety_supply_temp"` means the mode is
stored but the server refused to write because a guard is violated.

### `refresh`

Forces a sensor poll now (priority `POLL`). Rate limited to once per 60 s.
Returns `{"queued":true}`; values arrive as `sensor_update` events.

### `unsubscribe`

No params. Stops `sensor_update` events for this connection.

## Events (server → client)

### `sensor_update`

Sent only to subscribed connections, only for sensors in the subscription, and
only when a value **changed** (or on explicit `refresh`, where unchanged values
are still omitted). Several changes from one poll are batched into one event.

```json
{"event":"sensor_update","ts":…,"data":{"values":{"bt1_outdoor":-3.6,"degree_minutes":-130}}}
```

### `status`

Sent to all authenticated connections whenever any of these change: pump
online/offline, heating mode / active / suspended_reason, hot water mode,
watchdog `ha_alive`, a watchdog revert, server shutting down. `data` has the
same shape as `get_status` so clients can simply overwrite their copy.

### `shutdown`

Sent on graceful stop: `{"event":"shutdown","data":{"reason":"shutdown"}}`.
The client should reconnect with backoff.

## Error codes

| code | meaning |
|---|---|
| `auth_required` | command before successful `auth` |
| `auth_failed` | bad proof or unsupported proto |
| `unknown_command` | not in the allowlist |
| `invalid_params` | schema validation failed; `message` says which field |
| `pump_offline` | serial link down; command not attempted |
| `pump_timeout` | pump did not answer in time |
| `write_failed` | pump answered but read-back does not match |
| `rate_limited` | too frequent (`trigger_hot_water_boost`, `refresh`) |
| `rejected_safety` | reserved for commands refused outright by a guard |
| `internal` | unexpected server error (logged with traceback) |

## Timing summary

| What | Interval | Owner |
|---|---|---|
| HA heartbeat `ping` | 60 s | HA |
| Watchdog timeout | 300 s | server |
| Sensor poll (fast / full) | 60 s / 600 s | server |
| Degree-minute push while `enable`/`disable` | 600 s | server |
| Auth handshake deadline | 10 s | server |
| Reconnect backoff (HA → server) | 1 s … 60 s | HA |

## Versioning rules

* Adding commands, events, fields or sensors: no `proto` bump.
* Removing/renaming any of those or changing the auth scheme: `proto` bump,
  and the server keeps accepting the previous `proto` for at least one release.
