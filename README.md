# omini-plugin-openwrt

[Omini](https://github.com/riccardoalv/omini) plugin for **OpenWrt** routers and access points, through **ubus over HTTP** — the JSON-RPC API LuCI itself uses. Read-only: it only calls ubus methods that read, and never changes the device's configuration.

> **Experimental.** This plugin was built from OpenWrt's documentation and source, not tested on real hardware yet. Reports of what works (and what does not) on your device are very welcome.

## What it reads

| Data | ubus method | Used for |
|---|---|---|
| Hostname, model, OpenWrt version | `system board` (**required**) | The device on the map |
| Uptime, memory, swap, load, overlay (`/`) and `/tmp` usage | `system info` | Device health |
| Ports: status (carrier), **link speed / duplex** (`1000F`), MAC, traffic and error counters, bridge members, bridge VLANs (DSA) | `network.device status` | Ports on the map, link speed, traffic, VLANs |
| Logical interfaces (lan, wan...): IPs, default route → **WAN** with its gateway; PPPoE links on their port | `network.interface dump` | WAN nodes, IPs of the ports, router or AP |
| Host names (from ARP, DHCP, `/etc/ethers`) | `luci-rpc getHostHints` | Names of the devices behind the router |
| DHCP leases | `luci-rpc getDHCPLeases` | Names and addresses of clients |
| Wi-Fi networks (SSID, band) and **clients** with signal and link rates | `iwinfo devices`, `iwinfo info`, `iwinfo assoclist` | Wi-Fi clients on the map |
| ARP table | `file read` of `/proc/net/arp` | Which IP and MAC are on which interface |
| Temperatures (kernel thermal zones) | `file list` / `file read` under `/sys/class/thermal` | Device health (optional) |

The device is a **router** when one of its interfaces has a default route (or is a `wan*` interface that is down), else an **access point**.

Every method but `system board` is optional: when the ACL refuses it, or the device does not have it (no Wi-Fi, no LuCI), that data is skipped and the rest is still collected. **Test connection** lists the methods the ACL refused.

## Requirements

- OpenWrt **21.02 or later** with LuCI installed (it brings `uhttpd-mod-ubus`, which serves `/ubus`, and `rpcd-mod-luci` / `luci-rpc`). On a build without LuCI, install `uhttpd-mod-ubus` (and `rpcd-mod-iwinfo` for Wi-Fi).
- An rpcd login for Omini (below). Using `root` works too, but gives Omini far more access than it needs.

## Create a read-only user for Omini

rpcd logins are not system users: each is an entry in `/etc/config/rpcd` that maps a username and password to ACL groups ([ubus docs: ACLs](https://openwrt.org/docs/techref/ubus)). Over SSH on the router:

1. Create the ACL file `/usr/share/rpcd/acl.d/omini.json`. It grants **only read** access, and only to the methods listed above:

   ```json
   {
     "omini": {
       "description": "Omini: read-only access for the network map",
       "read": {
         "ubus": {
           "system": ["board", "info"],
           "network.device": ["status"],
           "network.interface": ["dump"],
           "luci-rpc": ["getHostHints", "getDHCPLeases"],
           "iwinfo": ["devices", "info", "assoclist"],
           "file": ["read", "list"]
         },
         "file": {
           "/proc/net/arp": ["read"],
           "/sys/class/thermal": ["list"],
           "/sys/class/thermal/thermal_zone*/type": ["read"],
           "/sys/class/thermal/thermal_zone*/temp": ["read"]
         }
       }
     }
   }
   ```

   The `file` section limits `file read` / `file list` to those exact paths: Omini cannot read any other file. Leave out the `file` entries (both the ubus methods and the paths) if you do not want ARP and temperatures.

2. Add the login to `/etc/config/rpcd`. The password is stored as a crypt hash made by `uhttpd -m`:

   ```sh
   uci add rpcd login
   uci set rpcd.@login[-1].username='omini'
   uci set rpcd.@login[-1].password="$(uhttpd -m 'a-long-random-password')"
   uci add_list rpcd.@login[-1].read='omini'
   uci commit rpcd
   /etc/init.d/rpcd restart
   ```

   The resulting section in `/etc/config/rpcd` looks like this — note there is **no `list write`**:

   ```
   config login
   	option username 'omini'
   	option password '$1$...'
   	list read 'omini'
   ```

   (Without `uhttpd`, e.g. with nginx, make the hash elsewhere with `openssl passwd -1 'a-long-random-password'`.)

3. In Omini, fill in the router's address (e.g. `https://192.168.1.1`), the username (`omini`) and the password, and click **Test connection**.

## Form fields

| Field | Default | |
|---|---|---|
| Address | — | The LuCI address, e.g. `https://192.168.1.1` (or `http://` if LuCI has no HTTPS; with no scheme, `https://` is used) |
| Username | `omini` | The rpcd login |
| Password | — | Secret: stored encrypted by Omini, never logged |
| Verify the TLS certificate | off | OpenWrt uses a self-signed certificate by default |

## How it works

- Every request is a JSON-RPC 2.0 `POST` to `<address>/ubus` ([ubus over HTTP](https://openwrt.org/docs/techref/ubus)). POST is how JSON-RPC carries a call; only methods that read are called.
- `session login` (with the username and password) returns a session id (`ubus_rpc_session`); every `call` carries it. The session is kept in the plugin's state folder and reused while it is valid — rpcd sessions expire after 5 minutes without use, and each use renews them. When ubus refuses a reused session (`Access denied`, -32002), the plugin logs in again once.
- Methods ([ubus reference](https://openwrt.org/docs/techref/ubus), [rpcd](https://openwrt.org/docs/techref/rpcd)):
  - `system board`, `system info` (procd): `load` values are scaled by 65536; `root` and `tmp` sizes are in KiB.
  - `network.device status`, `network.interface dump` (netifd): `speed` such as `1000F` (Mbps + duplex); `bridge-vlans` ports read `lan1` (untagged), `lan1:t` (tagged), `lan1:*` (PVID); a WAN is a logical interface with a `0.0.0.0/0` or `::/0` route, its `nexthop` is the gateway, and a PPPoE `l3_device` (`pppoe-wan`) runs on its `device`.
  - `iwinfo devices` / `info` / `assoclist` (rpcd-mod-iwinfo): band from the channel frequency (else the radio's modes), rates in kbit/s → Mbps. Only interfaces in AP mode (`Master`) list clients — a client-mode interface's upstream AP is not a client.
  - `luci-rpc getHostHints` / `getDHCPLeases` (LuCI's rpcd plugin).
  - `file read` / `file list` (rpcd's file plugin), restricted to the paths in the ACL.

## Known limitations

Built from documentation without hardware, so:

- **CPU usage** is not available through ubus (only load averages); Omini shows the load.
- **Port connector** (RJ45 / SFP) is not reported by netifd, so ports have none.
- **Firmware updates:** OpenWrt does not check for updates by itself; nothing is reported.
- **Temperatures** depend on the kernel's thermal zones (many SoCs have one, x86 often none) and on rpcd being able to read sysfs files; zones whose name has `cpu` are reported as CPU.
- **Bridge VLANs** come from netifd's `bridge-vlans` (DSA, OpenWrt 21.02+ bridge VLAN filtering); older swconfig switches are not read.
- **Wi-Fi traffic** per client: iwinfo gives byte counters, not current rates, so clients carry link rates and signal but no ↓/↑ traffic.
- A WAN that is **down** is recognized only by OpenWrt's naming convention (`wan`, `wan6`, `wanb`...) with an uplink protocol (DHCP, PPPoE, static...).
- The exact shape of answers may vary between OpenWrt releases; unknown or missing fields are left empty, never guessed.

## Development

The plugin uses [uv](https://docs.astral.sh/uv/) and expects the Omini repository next to it (the SDK is in `../omini/sdk/python`):

```bash
uv run pytest          # tests, against fixtures shaped like documented ubus answers (tests/fixtures)
uv run ruff check .    # lint
uv run ruff format .   # format
```

Run it from a local Omini without installing it: `OMINI_PLUGIN_DIRS=../omini-plugin-openwrt make run` in the Omini repository; every collection uses the current code.

## License

MIT
