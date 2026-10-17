# Auto-discovery

## Mechanism: mDNS / DNS-SD (zeroconf)

Home Assistant already runs a zeroconf browser and lets an integration declare
in its `manifest.json` which service types it wants to be notified about. Using
the same mechanism means zero extra infrastructure on the HA side and no UDP
broadcast code of our own.

### Service registration (server side)

* Library: `zeroconf` (python-zeroconf, the one HA itself uses), via its
  `AsyncZeroconf` API so it runs on our event loop.
* Service type: **`_nibe-usb._tcp.local.`** (DNS-SD limits the service name to 15 bytes, so the longer `_nibe-service-usb` is not allowed)
* Instance name: `<instance_name>._nibe-usb._tcp.local.` where
  `instance_name` is `<discovery.name>-<first 8 hex of server_id>`, e.g.
  `Nibe heat pump-6f1c2a9e` (control characters and dots removed, at most
  63 bytes). Zeroconf requires instance names to be unique on the link; the
  server_id suffix guarantees it even with two gateways.
* Port: the TCP listen port.
* Addresses: all IPv4 addresses of the host except loopback and link-local
  (IPv6 is not advertised). A watcher checks the addresses every 30 s and,
  when they change (DHCP renewal, Wi-Fi roam, cable plugged in), unregisters,
  recreates the zeroconf instance and registers again; it also retries if
  registration failed earlier (for example because the network was not up at
  boot). If `server.listen_host` is a specific address, only that address is
  advertised.
* TXT record (all values ASCII, kept short; total well under 400 bytes):

| key | example | purpose |
|---|---|---|
| `id` | `6f1c2a9e-…` | server_id UUID; HA unique_id, dedupe across IP changes |
| `proto` | `1` | wire protocol version; HA can refuse incompatible servers early |
| `ver` | `0.1.0` | server software version (diagnostics) |
| `auth` | `hmac-sha256` | auth scheme hint |
| `name` | `Nibe heat pump` | human-readable name shown in the HA discovery card (`discovery.name`) |
| `model` | `F1226-6 R` | pump model from `discovery.model`; omitted when empty |

The key itself is **never** in the TXT record.

* Registered right after the TCP server starts listening; unregistered (with
  goodbye packets) during graceful shutdown. If registration fails (no
  usable address, no multicast capable interface) the server logs a warning,
  keeps running and retries — discovery is a convenience, not a dependency.
  A successful registration is logged as
  `mDNS: advertising <name> on <addresses> port <port>`.
* `discovery.enabled = false` in config turns it off entirely.

### What the HA integration does with it (for context only)

`manifest.json` declares `"zeroconf": ["_nibe-usb._tcp.local."]`.
HA calls the integration's zeroconf discovery step with host, port and the TXT
properties. The integration sets `unique_id = properties["id"]`, updates the
stored host/port if the same id reappears on a new address, and asks the user
for the key. The HA side is out of scope here; this document only fixes the
contract: service type, TXT keys and their meaning.

## Fallback: manual host + port

The HA config flow offers a manual step where the user enters host (IP or DNS
name), port (default 9477) and the key. The server does not need anything
special for this; the `hello` event already carries `server_id` and `proto`, so
a manually added server gets the same unique id as a discovered one. This
covers:

* HA and the gateway on different VLANs/subnets with no mDNS reflector
* HA running in a container without host networking
* users who disable discovery

With manual setup, give the gateway a fixed address (DHCP reservation or a
DNS name); without discovery, HA cannot follow an address change.

## Why not alternatives

* **SSDP/UPnP**: HA supports it, but it is more verbose, needs an XML
  description document served over HTTP, and brings no benefit over mDNS.
* **Custom UDP broadcast beacon**: would need custom code on both sides and a
  second socket to secure; mDNS is already handled by HA.
* **Server-initiated discovery of HA**: wrong direction; the server should not
  need to know anything about HA.
