# Sensors and polling

## Sensor definitions

All sensors are declared in `sensors.yaml`. A copy ships with the package
(`src/nibe_service_usb/sensors.yaml`), and `poll.sensors_file` can point to
your own copy. The pump layer uses the `register` / `rt_field` / `ram` /
`type` / `scale` fields. The API layer uses the rest. Which values are
exposed, and why, is described in [sensor-selection.md](sensor-selection.md).

The file has two top-level lists, `sensors` and `alarms`. Order matters: the
poller groups registers into `RI` lines in file order.

```yaml
sensors:
  - name: bt1_outdoor                 # wire identifier, never renamed
    label: "Outdoor temperature (BT1)"
    register: 40004                   # RI fallback
    rt_field: 1                       # position in the `RT 1` line (preferred source)
    type: s16
    scale: 0.1
    unit: "°C"
    kind: temperature
    precision: 1
    tier: core
    device_class: temperature
    state_class: measurement

  - name: hw_comfort
    label: "Hot water comfort"
    register: 47041
    type: s8
    kind: enum
    options: {0: "eco", 1: "normal", 2: "lux"}
    tier: core
    device_class: enum

  - name: degree_minutes
    label: "Degree minutes"
    register: 40940
    type: s32
    scale: 0.1                        # raw = 10 x displayed DM
    unit: "DM"
    kind: number
    precision: 1
    tier: core

  - name: pi_running
    label: "Periodic increase running"
    ram: [0x2066718C, 3]              # RM address and length
    type: ram_timer_running
    kind: binary
    tier: core

  - name: bt25_ext_flow
    label: "External flow (BT25)"
    register: 40071
    rt_field: 25
    type: s16
    scale: 0.1
    unit: "°C"
    kind: temperature
    tier: core
    enabled: false                    # not fitted on the test unit

alarms:                               # flag registers folded into the `active_alarms` sensor
  - {number: 1, register: 45005, title: "Sensor fault BT1"}
```

Fields: `name`, `label`, `register`, `rt_field`, `ram`, `type` (`s8 u8 s16
u16 s32 u32`, or `ram_timer_running` / `ram_timer_seconds` / `ram_s32`),
`scale`, `unit`, `kind` (`temperature number enum binary duration
alarm_list`), `precision`, `options`, `tier` (`core settings identity
diagnostic`), `enabled`, `device_class`, `state_class`, `description`.
`nibe-service-usb check-config` validates the file and prints the counts per
tier.

Rules:

* `name` is the wire identifier. It is never renamed once released, because
  Home Assistant entity ids depend on it. New sensors can be added freely.
* Enum sensors are delivered as their string option, never the raw number.
  A value without an option is delivered as `"unknown_<n>"`.
* Binary sensors are delivered as JSON booleans.
* Temperature sentinels (`-32768` open/short, `-32767` over range) are
  delivered as `null`.
* `precision` rounds the published value and defines "changed": a 0.04 °C
  jitter on a 1-decimal sensor is not a change.
* The registers that commands change (`hw_comfort`, `degree_minutes`,
  `pi_enabled`) are also readable sensors, so Home Assistant sees the real
  state.
* Sensors with `enabled: false` are neither polled nor published.

## Polling

The poller (`core/poller.py`) runs two cadences:

| Cycle | Interval | Reads |
|---|---|---|
| **Fast** | `poll.fast_interval`, default **60 s** | All `core` sensors except the slow counters (`comp_starts`, `comp_hours`, `comp_hours_hw`): temperatures, degree minutes, compressor and pump states, hot water state, periodic-increase status. Also the alarm flags and `uptime`. |
| **Full** | `poll.interval`, default **600 s** | Everything in `core` including the counters, alarms, `uptime`. `settings` every `poll.settings_every`-th full cycle (default 6 = hourly). `diagnostic` if `poll.diagnostic = true`. |

* A full cycle also resets the fast timer, so the two never run back to back.
* Temperatures come from one `RT 1` line. Register sensors are grouped into
  `RI` lines of up to `pump.ri_batch_size` (9) registers. The 53 alarm flags
  take 6 `RI` lines and are folded into `active_alarms`. The periodic-increase
  timer is one `RM` line. A fast cycle is about 11 console lines, roughly 1–2 s
  of serial time per minute.
* Every line is one `POLL`-priority request on the pump queue, so
  `COMMAND` and `CONTROL` requests (user commands, degree-minute pushes)
  can run between the lines of a poll.
* When the pump comes online (at startup and after every reconnect), a
  health check and a full poll run immediately, so the first `subscribe` gets
  real values. `subscribe` itself triggers a poll if the last one is older
  than `poll.interval`.
* `uptime` is part of the fast set, so a display restart (uptime goes down)
  is noticed within a minute. The poller then runs a health check and the
  heating controller re-pushes its request.
* The poller collects the changed sensors of one cycle and publishes **one**
  `sensor_changed` batch at the end, so Home Assistant gets a single
  `sensor_update` event per cycle. A failed read keeps the old value and
  timestamp. A sensor that fails 3 reads in a row is published as `null`
  (unavailable).
* The `refresh` command starts an out-of-cycle full poll (rate limited to once
  per 60 s) and resets both timers.
* After a command write (hot water mode, periodic increase, degree-minute
  push) the affected registers are read back and published right away,
  without waiting for the next poll.
* The health check (identity, clock, EEPROM counters, uptime) runs at every
  (re)connect, after a display restart and daily at `pump.daily_check_time`.
  See [pump-interface.md](pump-interface.md).

## Change detection and delivery

* `State.set_sensor(name, value, ts)` compares the rounded new value with the
  stored one. Only a difference marks the sensor as changed. The timestamp
  is updated on every successful read regardless, so staleness checks
  (safety guards, snapshot freshness) work.
* `subscribe` returns the full snapshot (values and metadata) in its result.
  After that only changed values travel, as `sensor_update` events with a map
  `{name: value}`.
* If a subscriber's socket stays blocked for more than 10 s (write buffer
  full), the connection is closed. Home Assistant reconnects and
  re-subscribes, and receives a fresh snapshot. There are no per-client
  queues that can grow without limit.

## Pump offline behaviour

* Values keep their last reading and their old timestamp.
  `get_status.pump.online` is `false` and a `status` event has been sent, so
  Home Assistant can mark entities unavailable.
* Polls are skipped while the pump is offline. When it returns, a health
  check and a full poll run immediately.
