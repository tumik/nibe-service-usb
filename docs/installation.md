# Installation as a systemd service

This guide sets up the gateway as a permanent service on a Debian-based host
such as Raspberry Pi OS. It uses the unit file shipped in
[`systemd/nibe-service-usb.service`](../systemd/nibe-service-usb.service),
which expects this layout:

| What | Path |
|---|---|
| Code and virtual environment | `/opt/nibe-service-usb/`, executable `/opt/nibe-service-usb/.venv/bin/nibe-service-usb` |
| Configuration | `/etc/nibe-service-usb/config.toml` (`NIBE_SERVICE_USB_CONFIG`) |
| Data directory (key, server id) | `/var/lib/nibe-service-usb/` (`NIBE_SERVICE_USB_DATA_DIR`, created by systemd's `StateDirectory=`) |
| Service user | `nibe`, with supplementary group `dialout` |

If you have not tried the gateway yet, go through the
[install section of the README](../README.md#install) first. It is the fastest way to see
whether your pump answers at all.

Read the safety warning in the [README](../README.md) before you continue.

## 1. Prerequisites

```bash
python3 --version        # must be 3.11 or newer (Raspberry Pi OS / Debian 12 ship 3.11)
sudo apt install git python3-venv
ls -l /dev/serial/by-id/ # the pump's service port, usually -> ../../ttyACM0
```

## 2. Create the service user

The service runs as an unprivileged system user without a login shell:

```bash
sudo useradd --system --no-create-home --home-dir /var/lib/nibe-service-usb \
     --shell /usr/sbin/nologin --groups dialout nibe
```

The unit also sets `SupplementaryGroups=dialout`. The `--groups dialout`
above is needed so that `sudo -u nibe ...` (used below for `probe` and
`show-key`) can open the serial port too.

## 3. Install the code into `/opt`

```bash
sudo git clone https://github.com/tumik/nibe-service-usb.git /opt/nibe-service-usb
```

Then create the virtual environment at `/opt/nibe-service-usb/.venv` (that is
the path in `ExecStart=`). Pick **one** of the two options.

**Option A: venv and pip** (needs nothing beyond the system Python):

```bash
sudo python3 -m venv /opt/nibe-service-usb/.venv
sudo /opt/nibe-service-usb/.venv/bin/pip install -e /opt/nibe-service-usb
```

**Option B: uv** (installs the exact versions from `uv.lock`):

```bash
cd /opt/nibe-service-usb
sudo "$(command -v uv)" sync --locked --no-dev --python-preference only-system
```

> [!IMPORTANT]
> The unit sets `ProtectHome=true`, so the service cannot see anything under
> `/home` or `/root`. The interpreter of the venv must therefore not live in
> a home directory. A uv-managed Python under `~/.local/share/uv/python` does
> live there. `--python-preference only-system` makes uv use the system
> Python. If your system Python is older than 3.11, install a managed Python
> outside home directories instead, e.g.
> `sudo env UV_PYTHON_INSTALL_DIR=/opt/uv-python "$(command -v uv)" sync --locked --no-dev --python 3.12`.
> A service that fails with `status=203/EXEC` usually has this problem.

Check the installation:

```bash
/opt/nibe-service-usb/.venv/bin/nibe-service-usb --version
```

## 4. Configure

```bash
sudo install -d -m 0755 /etc/nibe-service-usb
sudo cp /opt/nibe-service-usb/config.example.toml /etc/nibe-service-usb/config.toml
sudo $EDITOR /etc/nibe-service-usb/config.toml
```

Every key is optional. Usually you only set `pump.port`, and maybe
`discovery.name`. A stable `/dev/serial/by-id/...` path is better than
`/dev/ttyACM0` when more than one ACM device is attached. The file holds no
secrets. See [configuration.md](configuration.md) for all keys.

Validate it. `/etc/nibe-service-usb/config.toml` is picked up automatically:

```bash
sudo -u nibe /opt/nibe-service-usb/.venv/bin/nibe-service-usb check-config
```

## 5. Create the data directory and probe the pump

systemd creates `/var/lib/nibe-service-usb` when the service starts. Create it
now so you can run commands as `nibe` before the first start:

```bash
sudo install -d -o nibe -g nibe -m 0700 /var/lib/nibe-service-usb
```

**Optional: keep the key from the quick start.** If you already paired Home
Assistant with a gateway running from your checkout, copy its key and server
id. Home Assistant then keeps working, with the same device and the same key:

```bash
sudo install -o nibe -g nibe -m 0600 ~/.local/share/nibe-service-usb/auth.key  /var/lib/nibe-service-usb/
sudo install -o nibe -g nibe -m 0600 ~/.local/share/nibe-service-usb/server_id /var/lib/nibe-service-usb/
```

Stop any gateway you started by hand (only one program can use the console),
then probe. This is read-only:

```bash
sudo -u nibe /opt/nibe-service-usb/.venv/bin/nibe-service-usb probe
```

## 6. Install and start the unit

```bash
sudo cp /opt/nibe-service-usb/systemd/nibe-service-usb.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nibe-service-usb
systemctl status nibe-service-usb
```

What the unit does, in short:

- runs `nibe-service-usb run` as `nibe` with the config and data directory
  from the table at the top;
- restarts it 5 s after a failure (`Restart=on-failure`);
- sandboxes it: read-only file system except the data directory
  (`ProtectSystem=strict`), no access to home directories, no new
  privileges, private `/tmp`, and access to `ttyACM` character devices only
  (`DeviceAllow=char-ttyACM rw`, `DevicePolicy=closed`).

To change a setting in the unit, use `sudo systemctl edit nibe-service-usb`
(a drop-in override) rather than editing the copied file.

## 7. Logs

The server logs to stderr, which systemd sends to the journal:

```bash
journalctl -u nibe-service-usb -f                 # follow
journalctl -u nibe-service-usb -b                 # since boot
journalctl -u nibe-service-usb --since "1 hour ago"
```

A healthy start looks like this:

```
... INFO nibe_service_usb.pump.client: pump online on /dev/ttyACM0
... INFO nibe_service_usb.core.heating: Degree-minute RAM address 0x2038af18 verified (DM raw ...)
... INFO nibe_service_usb.api.server: API listening on 0.0.0.0:9477
... INFO nibe_service_usb.app: listening on 0.0.0.0:9477
... INFO nibe_service_usb.api.server: Client ... authenticated (...)
... INFO nibe_service_usb.core.watchdog: First heartbeat received; HA is alive
```

For more detail (including raw console traffic), set `[log] level = "debug"`
in the config and restart. The key is never logged.

## 8. The key: show it and rotate it

The key is created by the server on its first start in
`/var/lib/nibe-service-usb/auth.key`, mode `0600`, owned by `nibe`. The log
only says that a new key was generated, not what it is. To display it:

```bash
sudo -u nibe /opt/nibe-service-usb/.venv/bin/nibe-service-usb show-key \
     --data-dir /var/lib/nibe-service-usb
```

`--data-dir` is needed here because the `NIBE_SERVICE_USB_DATA_DIR` setting
lives in the unit, not in your shell. Enter the key in the Home Assistant
integration (see the [README](../README.md#install), step 8).

To **rotate** the key (for example if it leaked):

```bash
sudo -u nibe /opt/nibe-service-usb/.venv/bin/nibe-service-usb rotate-key \
     --data-dir /var/lib/nibe-service-usb
sudo systemctl restart nibe-service-usb
```

The running server keeps the old key in memory until it restarts. After the
restart, Home Assistant fails to authenticate until you enter the new key in
the integration. The server id does not change, so Home Assistant keeps the
same device.

> [!NOTE]
> Always run `show-key` and `rotate-key` as `nibe` (`sudo -u nibe`). Run as
> root, they would create or replace `auth.key` with root as the owner, and
> the service could no longer read it. To repair:
> `sudo chown -R nibe:nibe /var/lib/nibe-service-usb`.

## 9. Debug website on the service

Add this to `/etc/nibe-service-usb/config.toml` and restart:

```toml
[debug_website]
enabled = true
port = 8477            # host stays 127.0.0.1
```

Then open it through an SSH tunnel from your desktop:
`ssh -L 8477:127.0.0.1:8477 you@gateway-host` and browse to
http://127.0.0.1:8477. The page has no authentication. Read the
[README section](../README.md#debug-website) before binding it to anything
other than localhost.

## 10. Upgrading

```bash
cd /opt/nibe-service-usb
sudo git pull
# Option A (pip):
sudo /opt/nibe-service-usb/.venv/bin/pip install -e /opt/nibe-service-usb
# Option B (uv):
sudo "$(command -v uv)" sync --locked --no-dev --python-preference only-system

sudo -u nibe /opt/nibe-service-usb/.venv/bin/nibe-service-usb check-config
sudo systemctl restart nibe-service-usb
```

- Unknown config keys are an error. If `check-config` complains after an
  upgrade, compare your file with the new `config.example.toml`. For example,
  `poll.control_interval` was replaced by `poll.fast_interval`.
- If `systemd/nibe-service-usb.service` changed, copy it again and run
  `sudo systemctl daemon-reload`.
- The key and server id in `/var/lib/nibe-service-usb` are kept across
  upgrades.

## 11. Uninstalling

```bash
sudo systemctl disable --now nibe-service-usb
sudo rm /etc/systemd/system/nibe-service-usb.service
sudo systemctl daemon-reload
sudo rm -rf /opt/nibe-service-usb /etc/nibe-service-usb /var/lib/nibe-service-usb
sudo userdel nibe
```

Remove the device from Home Assistant as well.

On the pump side nothing needs to be undone. Degree-minute writes go to RAM
and the pump's own control takes over as soon as the gateway stops writing.
The only persistent changes the gateway can make are the hot water comfort
mode you selected, and the periodic hot water increase enable bit, which a
triggered increase leaves switched on. Both are normal pump settings that
you can change on the display.
