# omini-plugin-openwrt

[Omini](https://github.com/riccardoalv/omini) plugin for **OpenWrt** routers and access points, through **ubus over HTTP** — the JSON-RPC API LuCI itself uses. It reads the router for the map and can **enable or disable a port**, change the **ports' VLANs**, create and delete **VLANs and their networks** (interface, DHCP range, firewall access), set **fixed addresses** and manage the **Wi-Fi network of each VLAN**; it changes nothing else.

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
| Which ports are disabled (`config device` sections with `enabled '0'`), the VLANs' names (`config bridge-vlan`), the interfaces and their addresses | `uci get` of the `network` config | Ports shown as "Disabled", VLANs, networks |
| Each network's DHCP range and the fixed addresses (`config dhcp`, `config host`) | `uci get` of the `dhcp` config | Networks, reservations (routers) |
| Each network's access (its firewall zone, rules and forwardings) | `uci get` of the `firewall` config | Networks (routers) |
| ARP table | `file read` of `/proc/net/arp` | Which IP and MAC are on which interface |
| Temperatures (kernel thermal zones) | `file list` / `file read` under `/sys/class/thermal` | Device health (optional) |

The device is a **router** when one of its interfaces has a default route (or is a `wan*` interface that is down), else an **access point**.

Every method but `system board` is optional: when the ACL refuses it, or the device does not have it (no Wi-Fi, no LuCI), that data is skipped and the rest is still collected. **Test connection** lists the methods the ACL refused.

## What it changes

**Enable or disable a port** (`port.enabled`), from the port's card in Omini. It works on physical ports of routers with a DSA switch (OpenWrt 21.02 and later: `lan1`, `lan2`, `wan`...) and on separate Ethernet ports (`eth1`). A port is disabled the way LuCI does it, with netifd's `enabled` option on the port's `config device` section in `/etc/config/network`:

1. `uci revert` `{"config": "network"}` — drops changes this session may have left staged (never the saved file).
2. To disable: `uci set` `{"config": "network", "section": "<the port's device section>", "values": {"enabled": "0"}}`, or, when the port has no section of its own, `uci add` `{"config": "network", "type": "device", "values": {"name": "lan1", "enabled": "0"}}`. To enable: `uci delete` `{"config": "network", "section": "<section>", "option": "enabled"}`. The port's other options, the bridges and the interfaces are not touched.
3. `uci apply` `{"rollback": true, "timeout": 30}` — rpcd saves the file and reloads the network, and **restores the previous file by itself after 30 seconds unless the change is confirmed**.
4. `network.device status` `{"name": "lan1"}`, read again (up to 15 s) until netifd shows the port disabled (`"present": false`) or enabled (`"present": true`).
5. Only then `uci confirm` `{}`: the change stays. If the port did not change, the plugin calls `uci rollback` `{}` instead; if the router stopped answering (the port was the way to it), it is not confirmed, and rpcd restores the previous configuration on its own.

A disabled port is down for netifd (it leaves it out of the bridge), stays down after a reboot, and appears as **Disabled** on the map. The plugin refuses the switch's CPU port (`eth0` on most routers, which carries every LAN port), bridges, VLANs and Wi-Fi interfaces, and routers whose switch uses **swconfig** (older devices with `config switch` sections), where ports are not separate devices.

**Wi-Fi per VLAN** (`wifi.set`, `wifi.delete`) works the same way on `/etc/config/wireless`: the access point of each radio is added, changed (name, network, password, on or off) or deleted, applied with rollback, read again and confirmed. A VLAN with no interface on the router gets one (a `bridge-vlan` tagged on the ports VLAN 1 has, and an unmanaged interface on it) only when the bridge already filters VLANs; otherwise the plugin asks to turn bridge VLAN filtering on in LuCI first. The password is written to the router only.

### VLANs of the ports (`port.vlans`, `vlan.set`, `vlan.delete`)

On a router whose bridge **filters VLANs** (DSA, OpenWrt 21.02 and later: `config bridge-vlan` sections in `/etc/config/network`), the plugin reports each bridge port's VLANs and changes them like the port's card in LuCI's *Bridge VLAN filtering* table:

- `port.vlans`: in every `bridge-vlan` section of the port's bridge, the port's entry is replaced by exactly what is asked: `lan1:u*` for the untagged VLAN (also the port's PVID), `lan1:t` for each tagged one, nothing for the others (`uci set` of the section's `ports` list). Other ports, and the bridge's own membership (the `local` option, the router's CPU), are never touched.
- `vlan.set`: a `config bridge-vlan` section `{device: br-lan, vlan: 30, description: <name>}` with no port (the router only), or the `description` of the existing one changed. The VLAN's name lives in `description`, which netifd ignores.
- `vlan.delete`: the VLAN's `bridge-vlan` sections. Refused for VLAN 1, and while a router interface rides on it (`br-lan.30`): delete its network first.

Each is applied with rollback and confirmed only once `network.device status` shows the bridge's VLANs as asked. **Bridge VLAN filtering is never turned on by the plugin**: with a bridge that has no `bridge-vlan` sections, it refuses and says how to do it in LuCI (*Network → Interfaces → Devices → br-lan → Configure… → Bridge VLAN filtering*: enable it, add VLAN 1 untagged and primary on the LAN ports, move the `lan` interface to `br-lan.1`, *Save & Apply*). Routers with **swconfig** switches are refused.

### A VLAN's network (`network.set`, `network.delete`) and fixed addresses (`reservation.set`, `reservation.delete`)

A router reports one network per statically addressed, private, LAN-side interface whose VLAN is known (`lan` is VLAN 1; `br-lan.30` or an 802.1Q device is VLAN 30), with its DHCP range and its access. `network.set` stages, in one change:

1. **The VLAN** (new networks only): a `bridge-vlan` on the bridge named as parent (or the LAN's), tagged on the parent port when it is one of the bridge's ports and on no port otherwise; or, on a port outside every bridge, a `config device` of type `8021q` (`eth2.30`). A bridge that does not filter VLANs is refused as above; the WAN is refused.
2. **The interface**: `config interface '<name>'` with `proto static`, the VLAN's device and the gateway address (`ipaddr`/`netmask`). An existing network (the LAN included) only gets its address changed; it never moves.
3. **DHCP** (`/etc/config/dhcp`): `config dhcp '<name>'` with `start` and `limit` from the range (offsets in the subnet), `leasetime 12h` and `dhcp_option` `3,<gateway>` and `6,<gateway>` (router and DNS). DHCP off sets `ignore '1'`.
4. **Access** (`/etc/config/firewall`), on a zone of the network's own (created with the interface's name):
   - *Internet only*: zone `input REJECT`, `forward REJECT`; rules `Omini: <name> DNS` (tcp/udp 53) and `Omini: <name> DHCP` (udp 67-68) to the router; a `forwarding` to the WAN's zone; and `Omini: <name> not to other networks` rejecting 10.0.0.0/8, 172.16.0.0/12 and 192.168.0.0/16 there.
   - *Whole network*: zone `input ACCEPT`, `forward ACCEPT` and a rule `Omini: <name> to everything` (`dest '*'`).
   - *No internet*: zone `input REJECT`, `forward REJECT` and the DNS and DHCP rules only.

   Choosing one replaces every rule and forwarding whose source is the network's zone. Access is read back the same way: those exact shapes give the choice, anything else is **Custom**, which leaves the rules alone. A zone shared with other networks is refused (its rules are theirs too), and so is *Internet only* or *No internet* on the network Omini reaches the router through (it would block LuCI from it).

`network.delete` removes the network's rules, forwardings and zone, its `config dhcp`, the interface, its 802.1Q device and its `bridge-vlan` when no port is in it any more (a VLAN ports still use stays, for `vlan.delete`). The LAN (VLAN 1) is never removed. `reservation.set` adds or changes a `config host` (`mac`, `ip`, `name`) in `/etc/config/dhcp`; `reservation.delete` removes it. All of it is applied with one `uci apply` with rollback, read back, and confirmed only when the router serves the network (or the reservation) as asked.

**Discovery between VLANs (mDNS)** is not managed: OpenWrt's reflectors (umdns, avahi) are packages set up by hand, so `mdns` on is refused with a note and off is ignored.

If another change applied from LuCI is still waiting to be confirmed, the plugin changes nothing and asks to try again. Collecting only reads: it never calls a uci method other than `uci get`.

## Requirements

- OpenWrt **21.02 or later** with LuCI installed (it brings `uhttpd-mod-ubus`, which serves `/ubus`, and `rpcd-mod-luci` / `luci-rpc`). On a build without LuCI, install `uhttpd-mod-ubus` (and `rpcd-mod-iwinfo` for Wi-Fi).
- An rpcd login for Omini (below). Using `root` works too, but gives Omini far more access than it needs.

## Create a user for Omini

rpcd logins are not system users: each is an entry in `/etc/config/rpcd` that maps a username and password to ACL groups ([ubus docs: ACLs](https://openwrt.org/docs/techref/ubus)). Over SSH on the router:

1. Create the ACL file `/usr/share/rpcd/acl.d/omini.json`. It grants read access to the methods listed above, and write access **only to the `network`, `wireless`, `dhcp` and `firewall` configurations** (ports, VLANs, networks, fixed addresses and Wi-Fi):

   ```json
   {
     "omini": {
       "description": "Omini: reads for the network map, changes ports, VLANs and networks",
       "read": {
         "ubus": {
           "system": ["board", "info"],
           "network.device": ["status"],
           "network.interface": ["dump"],
           "luci-rpc": ["getHostHints", "getDHCPLeases"],
           "iwinfo": ["devices", "info", "assoclist"],
           "file": ["read", "list"],
           "uci": ["get"]
         },
         "uci": ["network", "wireless", "dhcp", "firewall"],
         "file": {
           "/proc/net/arp": ["read"],
           "/sys/class/thermal": ["list"],
           "/sys/class/thermal/thermal_zone*/type": ["read"],
           "/sys/class/thermal/thermal_zone*/temp": ["read"]
         }
       },
       "write": {
         "ubus": {
           "uci": ["set", "add", "delete", "revert", "apply", "confirm", "rollback"]
         },
         "uci": ["network", "wireless", "dhcp", "firewall"]
       }
     }
   }
   ```

   The `uci` lists limit the uci methods to `/etc/config/network`, `/etc/config/wireless` (the Wi-Fi networks Omini manages per VLAN), `/etc/config/dhcp` (DHCP ranges and fixed addresses) and `/etc/config/firewall` (each network's access): Omini cannot read or change any other configuration (passwords, system, packages...). Leave `wireless`, or `dhcp` and `firewall`, out if Omini should not manage Wi-Fi, or networks (an access point needs neither `dhcp` nor `firewall`). The `file` section limits `file read` / `file list` to those exact paths: Omini cannot read any other file. Leave out the `file` entries (both the ubus methods and the paths) if you do not want ARP and temperatures.

   **Read-only instead:** leave out the `"write"` block (and the `list write` line below). The map works the same; enabling or disabling a port then fails with a message naming the ACL entries it needs.

2. Add the login to `/etc/config/rpcd`. The password is stored as a crypt hash made by `uhttpd -m`:

   ```sh
   uci add rpcd login
   uci set rpcd.@login[-1].username='omini'
   uci set rpcd.@login[-1].password="$(uhttpd -m 'a-long-random-password')"
   uci add_list rpcd.@login[-1].read='omini'
   uci add_list rpcd.@login[-1].write='omini'
   uci commit rpcd
   /etc/init.d/rpcd restart
   ```

   The resulting section in `/etc/config/rpcd` looks like this (without `list write` for a read-only login):

   ```
   config login
   	option username 'omini'
   	option password '$1$...'
   	list read 'omini'
   	list write 'omini'
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

- Every request is a JSON-RPC 2.0 `POST` to `<address>/ubus` ([ubus over HTTP](https://openwrt.org/docs/techref/ubus)). POST is how JSON-RPC carries a call; collecting calls only methods that read, and only a change calls the uci methods above.
- `session login` (with the username and password) returns a session id (`ubus_rpc_session`); every `call` carries it. The session is kept in the plugin's state folder and reused while it is valid — rpcd sessions expire after 5 minutes without use, and each use renews them. When ubus refuses a reused session (`Access denied`, -32002), the plugin logs in again once.
- Methods ([ubus reference](https://openwrt.org/docs/techref/ubus), [rpcd](https://openwrt.org/docs/techref/rpcd)):
  - `system board`, `system info` (procd): `load` values are scaled by 65536; `root` and `tmp` sizes are in KiB.
  - `network.device status`, `network.interface dump` (netifd): `speed` such as `1000F` (Mbps + duplex); `bridge-vlans` ports read `lan1` (untagged), `lan1:t` (tagged), `lan1:*` (PVID); a WAN is a logical interface with a `0.0.0.0/0` or `::/0` route, its `nexthop` is the gateway, and a PPPoE `l3_device` (`pppoe-wan`) runs on its `device`.
  - `iwinfo devices` / `info` / `assoclist` (rpcd-mod-iwinfo): band from the channel frequency (else the radio's modes), rates in kbit/s → Mbps. Only interfaces in AP mode (`Master`) list clients — a client-mode interface's upstream AP is not a client.
  - `luci-rpc getHostHints` / `getDHCPLeases` (LuCI's rpcd plugin).
  - `file read` / `file list` (rpcd's file plugin), restricted to the paths in the ACL.
  - `uci get` / `set` / `add` / `delete` / `revert` / `apply` / `confirm` / `rollback` (rpcd's uci plugin): changes are staged per session and saved by `apply`; `"present": false` in `network.device status` is netifd's state of a disabled device (it also leaves it out of the full status).

## Known limitations

Built from documentation without hardware, so:

- **CPU usage** is not available through ubus (only load averages); Omini shows the load.
- **Port connector** (RJ45 / SFP) is not reported by netifd, so ports have none.
- **Firmware updates:** OpenWrt does not check for updates by itself; nothing is reported.
- **Temperatures** depend on the kernel's thermal zones (many SoCs have one, x86 often none) and on rpcd being able to read sysfs files; zones whose name has `cpu` are reported as CPU.
- **Bridge VLANs** come from netifd's `bridge-vlans` (DSA, OpenWrt 21.02+ bridge VLAN filtering); older swconfig switches are not read, and their ports cannot be disabled.
- **Disabling a port** was checked against netifd's and rpcd's source, not on hardware. The switch's CPU port is recognized by netifd's `conduit` field or by sharing the DSA ports' MAC; on a device where neither holds, it is offered like any port.
- Without `uci get` in the ACL, ports netifd lists are shown enabled and a disabled port is missing from the map.
- **VLANs and networks** were built from netifd's, rpcd's and fw4's documentation and source, not tested on hardware: the bridge-vlan port syntax (`lan1:u*`, `lan1:t`), `config dhcp` start/limit and fw4 rules with `dest '*'` and `dest_ip` lists are as documented. A network's name is its interface's name (OpenWrt interfaces have no description). Wi-Fi networks on a deleted network's interface are left as they are.
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
