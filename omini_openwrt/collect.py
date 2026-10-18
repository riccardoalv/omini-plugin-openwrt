"""Turns ubus answers of an OpenWrt router or access point into an Omini device."""

from __future__ import annotations

import ipaddress
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from omini_sdk import (
    ArpEntry,
    Change,
    Config,
    ConfigBackup,
    Device,
    DhcpLease,
    DnsRecord,
    Gateway,
    Host,
    Interface,
    PausedDevice,
    PluginError,
    PortVlans,
    Storage,
    Temperature,
    Vlan,
    WirelessClient,
    log,
)

from omini_openwrt import backup as bk
from omini_openwrt import networks as nets
from omini_openwrt import services as svc
from omini_openwrt import vlans as vl
from omini_openwrt import wifi as wlan
from omini_openwrt.client import AccessDenied, Client, UbusError, Unavailable

PLUGIN_ID = "openwrt"

MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
SPEED = re.compile(r"^(\d+)([FH])?$")
THERMAL_ZONE = re.compile(r"^thermal_zone\d+$")

# netifd's "devtype" (the kernel's DEVTYPE) → Interface.type.
DEVTYPES = {
    "ethernet": "ethernet",
    "dsa": "ethernet",
    "bridge": "bridge",
    "vlan": "vlan",
    "wlan": "wireless",
    "bond": "lag",
    "ppp": "tunnel",
    "wireguard": "tunnel",
    "tun": "tunnel",
    "tap": "tunnel",
}

# Protocols of a logical interface that reach the internet: used to spot a WAN
# that is down (it has no default route then).
UPLINK_PROTOS = {"dhcp", "dhcpv6", "pppoe", "pppoa", "static", "qmi", "mbim", "ncm", "3g", "wwan"}

# ubus methods the plugin calls, as they go in the ACL file (see the README).
OPTIONAL = {
    "info": ("system", "info"),
    "devices": ("network.device", "status"),
    "interfaces": ("network.interface", "dump"),
    "hints": ("luci-rpc", "getHostHints"),
    "leases": ("luci-rpc", "getDHCPLeases"),
    "wifi": ("iwinfo", "devices"),
}

# UCI option values that mean "off" (uci stores booleans as strings).
OFF = {"0", "false", "off", "no", "disabled"}


def client_from(cfg: Config) -> Client:
    url, user, password = cfg.str("url"), cfg.str("username", "omini"), cfg.str("password")
    if not url or not password:
        raise PluginError("the address and the password are required")
    return Client(
        url, user, password, verify_tls=cfg.bool("verify_tls", False), state_dir=cfg.state_dir
    )


# --- helpers ---------------------------------------------------------------


def norm_mac(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    mac = value.strip().lower().replace("-", ":")
    if not MAC.match(mac) or mac == "00:00:00:00:00:00":
        return None
    return mac


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def counter(value: Any) -> int | None:
    n = as_int(value)
    return n if n is not None and n >= 0 else None


def parse_speed(value: Any) -> tuple[int | None, str | None]:
    """netifd's link speed, e.g. "1000F" → (1000, "full"); "-1" or none → unknown."""
    if not isinstance(value, str):
        return None, None
    m = SPEED.match(value.strip())
    if not m or int(m.group(1)) <= 0:
        return None, None
    duplex = {"F": "full", "H": "half"}.get(m.group(2) or "")
    return int(m.group(1)), duplex


def band_of(info: dict[str, Any]) -> str | None:
    """Wi-Fi band from the channel's frequency (MHz), else from the modes."""
    freq = as_int(info.get("frequency"))
    if freq:
        if 2400 <= freq < 2500:
            return "2.4ghz"
        if 5925 <= freq <= 7125:
            return "6ghz"
        if 4900 <= freq < 5925:
            return "5ghz"
        return None
    modes = {str(m).lower() for m in info.get("hwmodes") or []}
    if modes and modes <= {"b", "g", "n", "ax"} and modes & {"b", "g"}:
        return "2.4ghz"
    if modes and modes <= {"a", "n", "ac", "ax"} and modes & {"a", "ac"}:
        return "5ghz"
    return None


BAND_LABEL = {"2.4ghz": "2.4 GHz", "5ghz": "5 GHz", "6ghz": "6 GHz"}


def rate_mbps(side: Any) -> float | None:
    """iwinfo reports rates in kbit/s."""
    if not isinstance(side, dict):
        return None
    kbps = as_int(side.get("rate"))
    return round(kbps / 1000, 1) if kbps and kbps > 0 else None


def is_default_route(route: dict[str, Any]) -> bool:
    return route.get("target") in ("0.0.0.0", "::") and as_int(route.get("mask")) == 0


def lan_side(firewall: Any, name: str) -> bool:
    """Whether a logical interface is on the LAN side, where a default route
    (a dumb access point's gateway on its lan) is no WAN: its firewall zone
    neither masquerades nor is a forwarding's destination; in no zone (or
    without the firewall config), OpenWrt's name for it, "lan"."""
    zone = nets.zone_of(firewall, name)
    if zone is None:
        return name == "lan"
    if str(zone.get("masq", "0")).strip().lower() in nets.ON:
        return False
    return all(f.get("dest") != zone.get("name") for f in wlan.sections(firewall, "forwarding"))


def default_routes(li: dict[str, Any], firewall: Any) -> list[dict[str, Any]]:
    """The default routes that make a logical interface a WAN."""
    if lan_side(firewall, str(li.get("interface") or "")):
        return []
    return [r for r in li.get("route") or [] if isinstance(r, dict) and is_default_route(r)]


def addresses(iface: dict[str, Any]) -> list[str]:
    out = []
    for key in ("ipv4-address", "ipv6-address"):
        for a in iface.get(key) or []:
            addr, mask = a.get("address"), as_int(a.get("mask"))
            if not isinstance(addr, str) or mask is None or addr.startswith("fe80:"):
                continue
            out.append(f"{addr}/{mask}")
    return out


def subnet_of(cidrs: list[str]) -> str | None:
    for c in cidrs:
        try:
            net = ipaddress.ip_interface(c).network
        except ValueError:
            continue
        if net.version == 4:
            return str(net)
    return None


class Reader:
    """Calls ubus methods, remembering which ones the ACL refused."""

    def __init__(self, client: Client):
        self.client = client
        self.denied: list[str] = []

    def required(self, obj: str, method: str) -> Any:
        try:
            return self.client.call(obj, method)
        except AccessDenied as e:
            raise PluginError(
                f"the ACL of user {self.client.username!r} does not allow ubus "
                f"{obj} {method}: see the plugin's README"
            ) from e
        except UbusError as e:
            raise PluginError(f"{self.client.base} did not answer ubus {obj} {method}") from e

    def optional(
        self,
        obj: str,
        method: str,
        args: dict[str, Any] | None = None,
        report: str | None = "",
    ) -> Any:
        """Calls an optional method: None when it is refused or missing. A
        refusal is reported under `report` (default "obj method"; None: never)."""
        try:
            return self.client.call(obj, method, args)
        except AccessDenied:
            name = f"{obj} {method}" if report == "" else report
            if name and name not in self.denied:
                self.denied.append(name)
            return None
        except UbusError as e:
            log.debug("skipping %s", e)
            return None

    def file_read(self, path: str, report: bool = True) -> str | None:
        data = self.optional(
            "file", "read", {"path": path}, report=f"file read {path}" if report else None
        )
        text = data.get("data") if isinstance(data, dict) else None
        return text if isinstance(text, str) else None


# --- reading ---------------------------------------------------------------


def read_all(client: Client) -> tuple[dict[str, Any], list[str]]:
    r = Reader(client)
    raw: dict[str, Any] = {"board": r.required("system", "board")}
    for key, (obj, method) in OPTIONAL.items():
        raw[key] = r.optional(obj, method)

    wifi = []
    names = raw["wifi"].get("devices") if isinstance(raw["wifi"], dict) else None
    for dev in names or []:
        info = r.optional("iwinfo", "info", {"device": dev})
        if not isinstance(info, dict):
            continue
        assoc = None
        if str(info.get("mode", "")).lower() in ("master", "ap"):
            assoc = r.optional("iwinfo", "assoclist", {"device": dev})
        wifi.append((dev, info, assoc))
    raw["wifi"] = wifi

    # The network configuration: which ports are disabled (enabled '0').
    raw["config"] = r.optional("uci", "get", {"config": "network"}, report="uci get network")
    # The Wi-Fi networks and their VLANs (an access point's wireless config).
    raw["wireless"] = r.optional("uci", "get", {"config": "wireless"}, report=None)
    # A router's networks: their DHCP ranges and fixed addresses, and their access.
    raw["dhcp"] = r.optional("uci", "get", {"config": "dhcp"}, report="uci get dhcp")
    raw["firewall"] = r.optional("uci", "get", {"config": "firewall"}, report="uci get firewall")
    raw["arp"] = r.file_read("/proc/net/arp")
    raw["temps"] = read_temperatures(r)
    return raw, r.denied


def read_temperatures(r: Reader) -> list[tuple[str, str | None, str | None]]:
    """Thermal zones of the kernel: (zone, type, millidegrees). Skipped
    silently when the ACL does not list them."""
    listing = r.optional("file", "list", {"path": "/sys/class/thermal"}, report=None)
    entries = listing.get("entries") if isinstance(listing, dict) else None
    out = []
    for e in entries or []:
        zone = e.get("name") if isinstance(e, dict) else None
        if not isinstance(zone, str) or not THERMAL_ZONE.match(zone):
            continue
        base = f"/sys/class/thermal/{zone}"
        out.append((zone, r.file_read(f"{base}/type", False), r.file_read(f"{base}/temp", False)))
    return out


# --- mapping ---------------------------------------------------------------


def iface_type(name: str, st: dict[str, Any]) -> str | None:
    if name == "lo":
        return "loopback"
    kind = str(st.get("type") or "").lower()
    if kind == "bridge" or st.get("bridge-members") is not None:
        return "bridge"
    if kind in ("8021q", "8021ad", "vlan"):
        return "vlan"
    devtype = DEVTYPES.get(str(st.get("devtype") or "").lower())
    if devtype:
        return devtype
    if name.startswith(("pppoe-", "pppoa-", "wg", "tun", "gre", "l2tp-")):
        return "tunnel"
    return "other" if st.get("devtype") else None


def vlan_of(name: str, config: Any = None) -> tuple[str | None, int | None]:
    """A VLAN device's parent and VLAN: from its name (eth1.30, br-lan.30),
    else from its `config device` of type 8021q (`ifname`, `vid`)."""
    base, _, vid = name.rpartition(".")
    if base and vid.isdigit() and 1 <= int(vid) <= 4094:
        return base, int(vid)
    for s in config_sections(config, "device"):
        n = as_int(s.get("vid")) or 0
        if s.get("name") == name and s.get("type") in ("8021q", "8021ad") and 1 <= n <= 4094:
            return (str(s.get("ifname") or "") or None), n
    return None, None


def bridge_vlans(devices: dict[str, Any]) -> tuple[dict[str, PortVlans], list[tuple[str, int]]]:
    """Port membership of bridge VLANs (DSA), from netifd's "bridge-vlans":
    ports read "lan1" (untagged), "lan1:t" (tagged), "lan1:*" / "lan1:u*" (PVID)."""
    ports: dict[str, dict[str, Any]] = {}
    found = []
    for bridge, st in devices.items():
        for v in st.get("bridge-vlans") or []:
            vid = as_int(v.get("id")) if isinstance(v, dict) else None
            if not vid or not 1 <= vid <= 4094:
                continue
            found.append((bridge, vid))
            for p in v.get("ports") or []:
                port, _, flags = str(p).strip().partition(":")
                m = ports.setdefault(port, {"pvid": None, "untagged": [], "tagged": []})
                if "t" in flags:
                    m["tagged"].append(vid)
                else:
                    m["untagged"].append(vid)
                if "*" in flags:
                    m["pvid"] = vid
    out = {}
    for port, m in ports.items():
        untagged = m["pvid"] or (m["untagged"][0] if len(m["untagged"]) == 1 else None)
        out[port] = PortVlans(untagged=untagged, tagged=sorted(set(m["tagged"])) or None)
    return out, found


def config_sections(config: Any, kind: str) -> list[dict[str, Any]]:
    """Sections of one type from `uci get` of a whole config."""
    values = config.get("values") if isinstance(config, dict) else None
    if not isinstance(values, dict):
        return []
    return [s for s in values.values() if isinstance(s, dict) and s.get(".type") == kind]


def disabled_ports(config: Any) -> set[str]:
    """Ports disabled in the configuration: a plain `config device` section
    (no type: not a bridge or VLAN) with `option enabled '0'`."""
    out = set()
    for s in config_sections(config, "device"):
        name, enabled = s.get("name"), s.get("enabled")
        if s.get("type") or not isinstance(name, str) or not isinstance(enabled, str):
            continue
        if enabled.strip().lower() in OFF:
            out.add(name)
    return out


def bonds(config: Any) -> dict[str, list[str]]:
    """Bonding interfaces (LAGs) and their member ports: netifd's `config
    device` of type bonding (OpenWrt 24.10+, `list ports`), or proto-bonding's
    `config interface` with `proto 'bonding'` (`list slaves`, device
    bonding-<interface>)."""
    out: dict[str, list[str]] = {}
    for s in config_sections(config, "device"):
        name = s.get("name")
        if str(s.get("type") or "").lower() in ("bonding", "bond") and isinstance(name, str):
            out[name] = vl.as_list(s.get("ports"))
    for s in config_sections(config, "interface"):
        if str(s.get("proto") or "").lower() == "bonding" and isinstance(s.get(".name"), str):
            out[f"bonding-{s['.name']}"] = vl.as_list(s.get("slaves"))
    return out


def conduits(devices: dict[str, Any]) -> set[str]:
    """The CPU ports of DSA switches (eth0 on most routers): every LAN port's
    frames go through them, so they are not ports to turn off. netifd names
    them ("conduit") on recent releases; else they share the DSA ports' MAC."""
    named = {st.get("conduit") for st in devices.values()}
    dsa_macs = {
        norm_mac(st.get("macaddr"))
        for st in devices.values()
        if str(st.get("devtype") or "").lower() == "dsa"
    } - {None}
    out = {n for n in named if isinstance(n, str)}
    for name, st in devices.items():
        if str(st.get("devtype") or "").lower() == "ethernet" and (
            norm_mac(st.get("macaddr")) in dsa_macs
        ):
            out.add(name)
    return out


def build(raw: dict[str, Any], host: str | None) -> Device:
    board = raw["board"] if isinstance(raw["board"], dict) else {}
    info = raw["info"] if isinstance(raw["info"], dict) else {}
    devices = raw["devices"] if isinstance(raw["devices"], dict) else {}
    devices = {k: v for k, v in devices.items() if isinstance(v, dict)}
    logical = (raw["interfaces"] or {}).get("interface") if raw["interfaces"] else None
    logical = [i for i in logical or [] if isinstance(i, dict)]

    # Logical interfaces (lan, wan...) by the device that carries them.
    names_on: dict[str, list[str]] = {}
    ips_on: dict[str, list[str]] = {}
    parent_of: dict[str, str] = {}
    wan_devs: list[str] = []
    gateways: list[Gateway] = []
    device_ips: list[str] = []
    for li in logical:
        name = str(li.get("interface") or "")
        l3 = li.get("l3_device") or li.get("device")
        if not name or not isinstance(l3, str) or name == "loopback":
            continue
        names_on.setdefault(l3, []).append(name)
        cidrs = addresses(li)
        ips_on.setdefault(l3, []).extend(c for c in cidrs if c not in ips_on[l3])
        device_ips += [
            a["address"] for a in li.get("ipv4-address") or [] if isinstance(a.get("address"), str)
        ]
        dev = li.get("device")
        if isinstance(dev, str) and dev != l3:
            parent_of[l3] = dev  # pppoe-wan runs on eth1 (or a VLAN of it)
        routes = default_routes(li, raw.get("firewall"))
        up = li.get("up") is True
        down_uplink = not up and name.startswith("wan") and str(li.get("proto")) in UPLINK_PROTOS
        if not routes and not down_uplink:
            continue
        if l3 not in wan_devs:
            wan_devs.append(l3)
        hop = next(
            (r.get("nexthop") for r in routes if r.get("nexthop") not in (None, "0.0.0.0", "::")),
            None,
        )
        gateways.append(
            Gateway(name=name, interface=l3, address=hop, status="up" if up and routes else "down")
        )

    port_vlans, vlan_list = bridge_vlans(devices)
    # A port disabled in the configuration is not "present" for netifd, which
    # then leaves it out of `network.device status`: it is added from the
    # configuration, down and disabled.
    disabled = disabled_ports(raw.get("config"))
    cpu_ports = conduits(devices)
    lags = bonds(raw.get("config"))
    lag_of = {m: lag for lag, members in lags.items() for m in members}
    for name in disabled:
        devices.setdefault(name, {"present": False})

    interfaces: list[Interface] = []
    seen = set()
    for name in sorted(devices):
        st = devices[name]
        if name == "lo" or (st.get("present") is False and name not in disabled):
            continue
        seen.add(name)
        kind = "ethernet" if name in disabled else iface_type(name, st)
        if name in lags:
            kind = "lag"
        speed, duplex = parse_speed(st.get("speed"))
        up = st.get("carrier") if isinstance(st.get("carrier"), bool) else st.get("up")
        if name in disabled:
            up = False
        # Administrative state of physical ports: netifd lists a port it has
        # disabled as not present, so a present one is enabled.
        enabled = None
        if kind == "ethernet" and name not in cpu_ports:
            enabled = name not in disabled
        stats = st.get("statistics") if isinstance(st.get("statistics"), dict) else {}
        parent, vid = vlan_of(name, raw.get("config")) if kind == "vlan" else (None, None)
        members = st.get("bridge-members") or lags.get(name)
        interfaces.append(
            Interface(
                name=name,
                description=", ".join(names_on.get(name, [])) or None,
                type=kind,
                mac=norm_mac(st.get("macaddr")),
                ips=ips_on.get(name) or None,
                wan=True if name in wan_devs else None,
                parent=parent or parent_of.get(name),
                members=[str(m) for m in members] if members else None,
                up=up if isinstance(up, bool) else None,
                enabled=enabled,
                speed_mbps=speed if up is not False else None,
                duplex=duplex if up is not False else None,
                rx_bytes=counter(stats.get("rx_bytes")),
                tx_bytes=counter(stats.get("tx_bytes")),
                rx_errors=counter(stats.get("rx_errors")),
                tx_errors=counter(stats.get("tx_errors")),
                vlan=vid,
                vlans=port_vlans.get(name),
                lag=lag_of.get(name),
            )
        )
    # Layer-3 devices netifd does not list (e.g. pppoe-wan).
    for l3 in sorted(set(names_on) - seen - {"lo"}):
        if l3 not in wan_devs and not ips_on.get(l3):
            continue
        up = any(g.status == "up" for g in gateways if g.interface == l3) or None
        interfaces.append(
            Interface(
                name=l3,
                description=", ".join(names_on[l3]),
                type=iface_type(l3, {}),
                ips=ips_on.get(l3) or None,
                wan=True if l3 in wan_devs else None,
                parent=parent_of.get(l3),
                up=up,
            )
        )

    vlans = []
    vlan_names = vl.names(raw.get("config"))
    for bridge, vid in sorted(set(vlan_list), key=lambda v: (v[1], v[0])):
        dev = f"{bridge}.{vid}"
        vlans.append(
            Vlan(
                id=vid,
                name=vlan_names.get(vid) or ", ".join(names_on.get(dev, [])) or None,
                interface=dev if dev in devices or dev in names_on else None,
                subnet=subnet_of(ips_on.get(dev, [])),
            )
        )

    # Key: the MAC of the LAN, else the first port's.
    macs = []
    for i in interfaces:
        if i.mac and i.mac not in macs:
            macs.append(i.mac)
    lan_dev = next((d for d, ns in names_on.items() if "lan" in ns), None)
    lan_mac = next((i.mac for i in interfaces if i.name == lan_dev and i.mac), None)
    hostname = board.get("hostname") if isinstance(board.get("hostname"), str) else None
    key = lan_mac or (macs[0] if macs else None) or host or hostname
    if not key:
        raise PluginError(
            "OpenWrt reported no MAC address or host name: nothing to tell this device apart"
        )

    release = board.get("release") if isinstance(board.get("release"), dict) else {}
    os_version = " ".join(
        str(release[k]) for k in ("distribution", "version") if release.get(k)
    ).strip()

    mem = info.get("memory") if isinstance(info.get("memory"), dict) else {}
    mem_total, mem_used = mem_bytes(mem)
    swap = info.get("swap") if isinstance(info.get("swap"), dict) else {}
    load = [round(v / 65536, 2) for v in info.get("load") or [] if isinstance(v, int)][:3]

    leases, hosts = map_leases_and_hosts(raw["leases"], raw["hints"])
    wireless = map_wireless(raw["wifi"])
    extra: dict[str, Any] = {}
    if "wifi_networks" in Device.model_fields and raw.get("wireless"):
        extra["wifi_networks"] = wlan.wifi_networks(raw["wireless"], raw.get("config")) or None
    if gateways and raw.get("config"):
        extra.update(router_networks(raw, wan_names(raw["interfaces"], raw.get("firewall"))))
    if raw.get("dhcp"):
        records = svc.dns_records(raw["dhcp"])
        extra["dns_records"] = [DnsRecord(**r) for r in records] or None
        extra["dns_domain"] = svc.dns_domain(raw["dhcp"])
    if raw.get("firewall"):
        found = svc.paused(raw["firewall"])
        extra["paused"] = [PausedDevice(**p) for p in found] if found else None

    return Device(
        key=key,
        name=str(board.get("hostname") or host or "OpenWrt"),
        host=host,
        role="router" if any(g for g in gateways) else "ap",
        model=board.get("model") or None,
        os_version=os_version or None,
        uptime_s=counter(info.get("uptime")),
        mem_pct=mem_pct(mem),
        mem_total_bytes=mem_total,
        mem_used_bytes=mem_used,
        swap_pct=pct_used(swap.get("total"), swap.get("free")),
        load_avg=load or None,
        temperatures=map_temperatures(raw["temps"]) or None,
        storage=map_storage(info) or None,
        macs=macs or None,
        ips=list(dict.fromkeys(device_ips)) or None,
        interfaces=interfaces or None,
        arp=parse_arp(raw["arp"]) if raw["arp"] else None,
        ndp=map_ndp(raw["hints"]),
        dhcp_leases=leases,
        wireless_clients=wireless,
        gateways=gateways or None,
        hosts=hosts,
        vlans=vlans or None,
        **extra,
    )


def wan_names(dump: Any, firewall: Any = None) -> set[str]:
    """The WANs' logical interfaces and their devices: a default route off the
    LAN side (see lan_side), or a wan* uplink that is down."""
    logical = dump.get("interface") if isinstance(dump, dict) else None
    out: set[str] = set()
    for li in logical or []:
        if not isinstance(li, dict):
            continue
        name = str(li.get("interface") or "")
        routes = default_routes(li, firewall)
        down = (
            li.get("up") is not True
            and name.startswith("wan")
            and str(li.get("proto")) in UPLINK_PROTOS
        )
        if routes or down:
            out.add(name)
            out |= {d for d in (li.get("l3_device"), li.get("device")) if isinstance(d, str)}
    return out


def router_networks(raw: dict[str, Any], wans: set[str]) -> dict[str, Any]:
    """Each VLAN's network (interface, gateway, DHCP, access) and the DHCP's
    fixed addresses, from the network, dhcp and firewall configs."""
    found = nets.networks(raw["config"], raw.get("dhcp"), raw.get("firewall"), wans)
    out: dict[str, Any] = {"networks": found or None}
    if raw.get("dhcp"):
        out["dhcp_reservations"] = nets.reservations(raw["dhcp"]) or None
    return out


def pct_used(total: Any, free: Any) -> float | None:
    t, f = as_int(total), as_int(free)
    if not t or t <= 0 or f is None:
        return None
    return round(min(100.0, max(0.0, (t - f) * 100 / t)), 1)


def mem_available(mem: dict[str, Any]) -> int | None:
    available = as_int(mem.get("available"))
    if available is None:
        parts = [as_int(mem.get(k)) for k in ("free", "buffered", "cached")]
        if parts[0] is None:
            return None
        available = sum(p or 0 for p in parts)
    return available


def mem_pct(mem: dict[str, Any]) -> float | None:
    return pct_used(mem.get("total"), mem_available(mem))


def mem_bytes(mem: dict[str, Any]) -> tuple[int | None, int | None]:
    """procd's memory sizes are in bytes: (total, used = total - available)."""
    total, available = as_int(mem.get("total")), mem_available(mem)
    if not total or total <= 0:
        return None, None
    used = total - available if available is not None and 0 <= available <= total else None
    return total, used


def map_ndp(hints_raw: Any) -> list[ArpEntry] | None:
    """IPv6 neighbors: the ip6addrs getHostHints lists for each MAC."""
    out = []
    for raw_mac, hint in (hints_raw or {}).items() if isinstance(hints_raw, dict) else []:
        mac = norm_mac(raw_mac)
        if not mac or not isinstance(hint, dict):
            continue
        for ip in hint.get("ip6addrs") or []:
            try:
                if isinstance(ip, str) and ipaddress.ip_address(ip).version == 6:
                    out.append(ArpEntry(ip=ip, mac=mac))
            except ValueError:
                continue
    return out or None


def map_storage(info: dict[str, Any]) -> list[Storage]:
    """system info reports "root" (the overlay) and "tmp" in KiB."""
    out = []
    for key, mount in (("root", "/"), ("tmp", "/tmp")):
        fs = info.get(key)
        if not isinstance(fs, dict):
            continue
        total, used = as_int(fs.get("total")), as_int(fs.get("used"))
        if not total:
            continue
        out.append(
            Storage(
                mount=mount,
                total_bytes=total * 1024,
                used_bytes=used * 1024 if used is not None and used >= 0 else None,
            )
        )
    return out


def map_temperatures(zones: list[tuple[str, str | None, str | None]]) -> list[Temperature]:
    out = []
    for zone, kind, value in zones:
        milli = as_int((value or "").strip())
        if milli is None:
            continue
        sensor = (kind or "").strip() or zone
        out.append(
            Temperature(
                sensor=sensor,
                kind="cpu" if "cpu" in sensor.lower() else "other",
                celsius=round(milli / 1000, 1),
            )
        )
    return out


def parse_arp(text: str) -> list[ArpEntry]:
    """/proc/net/arp: IP, HW type, flags, MAC, mask, device. Flags 0x0 are
    incomplete entries."""
    out = []
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 6 or cols[2] == "0x0":
            continue
        mac = norm_mac(cols[3])
        if mac:
            out.append(ArpEntry(ip=cols[0], mac=mac, interface=cols[5]))
    return out


def map_leases_and_hosts(
    leases_raw: Any, hints_raw: Any
) -> tuple[list[DhcpLease] | None, list[Host] | None]:
    now = datetime.now(timezone.utc)
    leases = []
    names: dict[str, str] = {}
    rows = leases_raw.get("dhcp_leases") if isinstance(leases_raw, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        mac, ip = norm_mac(row.get("macaddr")), row.get("ipaddr")
        if not mac or not isinstance(ip, str):
            continue
        name = row.get("hostname") if isinstance(row.get("hostname"), str) else None
        if name:
            names[mac] = name
        secs = as_int(row.get("expires"))
        leases.append(
            DhcpLease(
                ip=ip,
                mac=mac,
                hostname=name or None,
                expires_at=now + timedelta(seconds=secs) if secs and secs > 0 else None,
            )
        )

    hosts = []
    for raw_mac, hint in (hints_raw or {}).items() if isinstance(hints_raw, dict) else []:
        mac = norm_mac(raw_mac)
        if not mac or not isinstance(hint, dict):
            continue
        name = hint.get("name") if isinstance(hint.get("name"), str) else None
        name = name or names.get(mac)
        for ip in hint.get("ipaddrs") or []:
            if isinstance(ip, str):
                hosts.append(
                    Host(ip=ip, mac=mac, hostnames=[name] if name else None, sources=[PLUGIN_ID])
                )
    return leases or None, hosts or None


def map_wireless(wifi: list[tuple[str, dict[str, Any], Any]]) -> list[WirelessClient] | None:
    out = []
    for dev, info, assoc in wifi:
        ssid = info.get("ssid") if isinstance(info.get("ssid"), str) else None
        band = band_of(info)
        label = " · ".join(x for x in (ssid or dev, BAND_LABEL.get(band or "")) if x)
        results = assoc.get("results") if isinstance(assoc, dict) else None
        for c in results or []:
            mac = norm_mac(c.get("mac")) if isinstance(c, dict) else None
            if not mac:
                continue
            signal = as_int(c.get("signal"))
            out.append(
                WirelessClient(
                    mac=mac,
                    interface=label,
                    ssid=ssid,
                    band=band,
                    signal_dbm=signal if signal is not None and signal < 0 else None,
                    tx_rate_mbps=rate_mbps(c.get("tx")),
                    rx_rate_mbps=rate_mbps(c.get("rx")),
                )
            )
    return out or None


# --- plugin entry points ---------------------------------------------------


def collect(cfg: Config) -> list[Device]:
    client = client_from(cfg)
    try:
        raw, denied = read_all(client)
    finally:
        client.close()
    if denied:
        log.info("the ACL does not allow: %s", ", ".join(denied))
    return [build(raw, urlparse(client.base).hostname)]


def test(cfg: Config) -> str:
    client = client_from(cfg)
    try:
        raw, denied = read_all(client)
    finally:
        client.close()
    dev = build(raw, urlparse(client.base).hostname)
    msg = f"Connected to {dev.name} ({dev.os_version or 'OpenWrt'})."
    if denied:
        msg += " Not allowed by the ACL: " + ", ".join(denied) + "."
    return msg


def backup(cfg: Config) -> list[ConfigBackup]:
    """The router's uci configs (network, wireless, dhcp, firewall, system) as
    `uci export` prints them, without secrets; configs the ACL does not let
    Omini read are left out."""
    client = client_from(cfg)
    try:
        raw, _ = read_all(client)
        system = Reader(client).optional("uci", "get", {"config": "system"}, report=None)
    finally:
        client.close()
    dev = build(raw, urlparse(client.base).hostname)
    configs = {
        "network": raw.get("config"),
        "wireless": raw.get("wireless"),
        "dhcp": raw.get("dhcp"),
        "firewall": raw.get("firewall"),
        "system": system,
    }
    content = bk.render(configs)
    if not content:
        raise PluginError(
            f"the ACL of user {client.username!r} does not allow reading any uci config "
            '(it needs "read": {"ubus": {"uci": ["get"]}, "uci": ["network", ...]}): '
            "see the plugin's README"
        )
    return [ConfigBackup(device=dev.key, format="text", content=content)]


# --- changes ---------------------------------------------------------------

# How long rpcd waits for the confirmation before it restores the previous
# configuration by itself (`uci apply` with rollback), and how long the
# plugin waits for netifd to bring the port down or up after the reload.
ROLLBACK_S = 30
VERIFY_S = 15
VERIFY_PAUSE_S = 1.0

WRITE_ACL = (
    'it needs "write": {"ubus": {"uci": ["set", "add", "delete", "revert", "apply", '
    '"confirm", "rollback"]}, "uci": ["network", "wireless", "dhcp", "firewall"]} in its '
    "ACL file and `list write 'omini'` in its login (see the plugin's README)"
)


def read_acl(config: str) -> str:
    """What the ACL needs to read one uci config."""
    return (
        f'it needs "read": {{"ubus": {{"uci": ["get"]}}, "uci": ["{config}"]}} '
        "in its ACL file (see the plugin's README)"
    )


def _write(c: Client, method: str, args: dict[str, Any]) -> Any:
    """A uci call that changes the configuration; a refusal names the ACL."""
    try:
        return c.call("uci", method, args)
    except AccessDenied as e:
        raise PluginError(
            f"the ACL of user {c.username!r} does not allow changes (ubus uci {method}): "
            + WRITE_ACL
        ) from e
    except UbusError as e:
        raise PluginError(f"OpenWrt refused uci {method}: {e} (nothing was changed)") from e


def port_present(c: Client, port: str) -> bool | None:
    """Whether netifd has the port up for use (`present`): False once it is
    disabled in the configuration. None when the answer says nothing."""
    try:
        st = c.call("network.device", "status", {"name": port})
    except Unavailable:
        return None
    present = st.get("present") if isinstance(st, dict) else None
    return present if isinstance(present, bool) else None


def check_port(raw_devices: Any, config: Any, port: str) -> None:
    """Refuses what is not a physical port of this router."""
    if config_sections(config, "switch"):
        raise PluginError(
            "this router's switch uses swconfig (an older driver): its ports are not "
            "separate devices, and the plugin cannot disable one alone"
        )
    devices = raw_devices if isinstance(raw_devices, dict) else {}
    devices = {k: v for k, v in devices.items() if isinstance(v, dict)}
    if port in disabled_ports(config):
        return
    st = devices.get(port)
    if st is None:
        raise PluginError(f"OpenWrt has no port {port}")
    if iface_type(port, st) != "ethernet":
        raise PluginError(f"{port} is not a physical port: only ports can be disabled")
    if port in conduits(devices):
        raise PluginError(
            f"{port} is the switch's CPU port: disabling it would take down every LAN port"
        )


def apply(cfg: Config, change: Change) -> list[Device]:
    """Enables or disables a port (port.enabled) the way LuCI saves a change:
    stages `enabled '0'` on the port's `config device` section (removes it to
    enable), applies it with rollback (rpcd saves it and reloads the network,
    and restores the previous file by itself unless confirmed), reads the
    port again and confirms only when netifd shows it disabled (or enabled).
    Nothing else in the configuration is touched. Returns the router as read
    after the change."""
    if change.kind in ("wifi.set", "wifi.delete"):
        return apply_wifi(cfg, change)
    if change.kind in ("port.vlans", "vlan.set", "vlan.delete"):
        return apply_vlans(cfg, change)
    if change.kind in ("network.set", "network.delete", "reservation.set", "reservation.delete"):
        return apply_networks(cfg, change)
    if change.kind in ("dns.set", "dns.delete", "pause.set", "pause.delete"):
        return apply_services(cfg, change)
    if change.kind == "wol.send":
        return apply_wol(cfg, change)
    if change.kind != "port.enabled" or not isinstance(change.value, bool):
        raise PluginError(f"this plugin cannot apply {change.kind} = {change.value!r}")
    port, value = change.port or "", change.value
    c = client_from(cfg)
    try:
        # A session of its own: staged changes and the pending apply belong
        # to it, and a refusal then means the ACL, not an expired session.
        c.login()
        r = Reader(c)
        devices = r.required("network.device", "status")
        try:
            config = c.call("uci", "get", {"config": "network"})
        except AccessDenied as e:
            raise PluginError(
                f"the ACL of user {c.username!r} does not allow ubus uci get on the network "
                "config: " + read_acl("network")
            ) from e
        check_port(devices, config, port)
        if (port not in disabled_ports(config)) == value:
            return collect(cfg)

        # Only this session's staged changes: the saved file is untouched.
        _write(c, "revert", {"config": "network"})
        sections = [
            s
            for s in config_sections(config, "device")
            if s.get("name") == port and not s.get("type") and isinstance(s.get(".name"), str)
        ]
        if not value:
            if sections:
                for s in sections:
                    _write(
                        c,
                        "set",
                        {"config": "network", "section": s[".name"], "values": {"enabled": "0"}},
                    )
            else:
                _write(
                    c,
                    "add",
                    {
                        "config": "network",
                        "type": "device",
                        "values": {"name": port, "enabled": "0"},
                    },
                )
        else:
            for s in sections:
                if str(s.get("enabled", "")).strip().lower() in OFF:
                    _write(
                        c,
                        "delete",
                        {"config": "network", "section": s[".name"], "option": "enabled"},
                    )

        try:
            c.call("uci", "apply", {"rollback": True, "timeout": ROLLBACK_S})
        except AccessDenied as e:
            _quietly(c, "revert", {"config": "network"})
            raise PluginError(
                "OpenWrt has another change waiting to be confirmed (applied from LuCI?): "
                "nothing was changed, try again in a minute"
            ) from e
        except UbusError as e:
            _quietly(c, "revert", {"config": "network"})
            raise PluginError(f"OpenWrt did not apply the change: {e} (nothing was changed)") from e

        try:
            ok = wait_for(c, port, value)
        except PluginError as e:
            raise PluginError(
                f"lost OpenWrt after changing {port}: it restores the previous configuration "
                f"by itself in {ROLLBACK_S} s ({e})"
            ) from e
        if not ok:
            _quietly(c, "rollback", {})
            _quietly(c, "revert", {"config": "network"})
            raise PluginError(
                f"OpenWrt did not {'enable' if value else 'disable'} {port} "
                "(rolled back, not saved)"
            )
        try:
            c.call("uci", "confirm", {})
        except UbusError as e:
            raise PluginError(
                f"could not confirm the change to {port}: OpenWrt restores the previous "
                f"configuration in {ROLLBACK_S} s"
            ) from e
    finally:
        c.close()
    return collect(cfg)


def wifi_vlan(value: dict[str, Any]) -> int:
    """The VLAN of a wifi.set: none is the untagged LAN (VLAN 1)."""
    return 1 if value.get("vlan") is None else vl.vlan_id(value, "vlan", "wifi.set")


def apply_wifi(cfg: Config, change: Change) -> list[Device]:
    """wifi.set: a Wi-Fi network on every radio of the router (its name, the
    VLAN's interface, the password, on or off); wifi.delete removes it. Staged
    in this session, applied with rollback, read again (name, VLAN, on or
    off), then confirmed — as LuCI saves a change."""
    value = change.value if isinstance(change.value, dict) else {}
    ssid = str(value.get("ssid") or "")
    previous = value.get("previous")
    if not ssid:
        raise PluginError(f"{change.kind} needs the network's name (ssid)")
    vid = wifi_vlan(value)
    names = ["wireless", "network"]
    c = client_from(cfg)
    try:
        c.login()
        configs = read_configs(c, names)
        wireless, network = configs["wireless"], configs["network"]
        aps = [
            s
            for s in wlan.sections(wireless, "wifi-iface")
            if s.get("mode", "ap") == "ap" and s.get("ssid") in (ssid, previous)
        ]
        if change.kind == "wifi.delete":
            if not aps:
                raise PluginError(f'the router has no Wi-Fi network "{ssid}"')
            ops: list[tuple[str, dict[str, Any]]] = [
                ("delete", {"config": "wireless", "section": s[".name"]}) for s in aps
            ]
        else:
            ops = stage_wifi(wireless, network, value, aps)
        enabled = value.get("enabled")

        def done() -> bool:
            got = read_configs(c, names)
            by = {w["ssid"]: w for w in wlan.wifi_networks(got["wireless"], got["network"])}
            if change.kind == "wifi.delete":
                return ssid not in by
            w = by.get(ssid)
            if w is None or (previous and previous != ssid and previous in by):
                return False
            if w["vlan"] != (vid if vid != 1 else None):
                return False
            return enabled is None or w["enabled"] == bool(enabled)

        commit(c, names, ops, done, f'the Wi-Fi network "{ssid}"')
    finally:
        c.close()
    return collect(cfg)


def stage_wifi(
    wireless: Any, network: Any, value: dict[str, Any], aps: list[dict[str, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    """The uci writes of a wifi.set: the VLAN's interface when missing, then
    each radio's access point."""
    ops: list[tuple[str, dict[str, Any]]] = []
    # No VLAN is the untagged LAN (VLAN 1): an existing network moves back to it.
    iface, writes = wlan.plan_interface(network, wifi_vlan(value))
    for kind, values in writes:
        name = values.pop("name", None)
        args: dict[str, Any] = {"config": "network", "type": kind, "values": values}
        if name:
            args["name"] = name
        ops.append(("add", args))
    options: dict[str, Any] = {"ssid": str(value["ssid"]), "network": iface}
    if value.get("password"):
        options["key"] = str(value["password"])
    if value.get("enabled") is not None:
        options["disabled"] = "0" if value["enabled"] else "1"
    radios = wlan.sections(wireless, "wifi-device")
    if aps:
        for s in aps:
            # Each access point keeps its own encryption; only an open one
            # gets one with the password.
            mine = dict(options)
            if value.get("password") and str(s.get("encryption") or "none") in ("none", "owe"):
                radio = next((r for r in radios if r.get(".name") == s.get("device")), None)
                mine["encryption"] = wlan.encryption_for(radio)
            ops.append(("set", {"config": "wireless", "section": s[".name"], "values": mine}))
        return ops
    if not value.get("password"):
        raise PluginError("a new Wi-Fi network needs a password")
    if not radios:
        raise PluginError("the router has no Wi-Fi radio")
    for r in radios:
        ops.append(
            (
                "add",
                {
                    "config": "wireless",
                    "type": "wifi-iface",
                    "values": {
                        "device": r[".name"],
                        "mode": "ap",
                        "encryption": wlan.encryption_for(r),
                        **options,
                    },
                },
            )
        )
    return ops


def wait_for(c: Client, port: str, enabled: bool) -> bool:
    """Reads the port until netifd shows it in the new state (the network
    reload runs a moment after `uci apply`)."""
    deadline = time.monotonic() + VERIFY_S
    while True:
        if port_present(c, port) is enabled:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(VERIFY_PAUSE_S)


def _quietly(c: Client, method: str, args: dict[str, Any]) -> None:
    try:
        c.call("uci", method, args)
    except (UbusError, PluginError) as e:
        log.warning("uci %s failed: %s", method, e)


# --- VLANs and networks -------------------------------------------------------


def read_configs(c: Client, names: list[str]) -> dict[str, Any]:
    """`uci get` of whole configs; a refusal names the ACL entries."""
    out = {}
    for name in names:
        try:
            out[name] = c.call("uci", "get", {"config": name})
        except AccessDenied as e:
            raise PluginError(
                f"the ACL of user {c.username!r} does not allow reading the {name} config: "
                + read_acl(name)
            ) from e
        except UbusError as e:
            raise PluginError(f"OpenWrt did not answer uci get {name}: {e}") from e
    return out


def wait_until(check: Any) -> bool:
    """Calls check until it holds (the reload runs a moment after `uci
    apply`); an error answer counts as not yet. Losing the router raises."""
    deadline = time.monotonic() + VERIFY_S
    while True:
        try:
            if check():
                return True
        except UbusError as e:
            log.debug("not yet: %s", e)
        if time.monotonic() >= deadline:
            return False
        time.sleep(VERIFY_PAUSE_S)


def commit(
    c: Client, configs: list[str], ops: list[tuple[str, dict[str, Any]]], done: Any, what: str
) -> None:
    """Stages ops in this session, applies them with rollback, waits until
    done() holds and only then confirms; else rolls back (or, when the router
    stopped answering, lets rpcd restore the previous files by itself)."""
    for name in configs:
        _write(c, "revert", {"config": name})
    for method, args in ops:
        _write(c, method, args)
    try:
        c.call("uci", "apply", {"rollback": True, "timeout": ROLLBACK_S})
    except AccessDenied as e:
        for name in configs:
            _quietly(c, "revert", {"config": name})
        raise PluginError(
            "OpenWrt has another change waiting to be confirmed (applied from LuCI?): "
            "nothing was changed, try again in a minute"
        ) from e
    except UbusError as e:
        for name in configs:
            _quietly(c, "revert", {"config": name})
        raise PluginError(f"OpenWrt did not apply {what}: {e} (nothing was changed)") from e
    try:
        ok = wait_until(done)
    except PluginError as e:
        raise PluginError(
            f"lost OpenWrt after changing {what}: it restores the previous configuration "
            f"by itself in {ROLLBACK_S} s ({e})"
        ) from e
    if not ok:
        _quietly(c, "rollback", {})
        for name in configs:
            _quietly(c, "revert", {"config": name})
        raise PluginError(f"OpenWrt did not apply {what} (rolled back, not saved)")
    try:
        c.call("uci", "confirm", {})
    except UbusError as e:
        raise PluginError(
            f"could not confirm {what}: OpenWrt restores the previous configuration "
            f"in {ROLLBACK_S} s"
        ) from e


def tags(pv: PortVlans | None) -> tuple[int | None, list[int]]:
    if pv is None:
        return None, []
    return pv.untagged, sorted(int(getattr(t, "root", t)) for t in pv.tagged or [])


def running_vlans(c: Client) -> list[tuple[str, int]]:
    status = c.call("network.device", "status")
    return bridge_vlans(status if isinstance(status, dict) else {})[1]


def apply_vlans(cfg: Config, change: Change) -> list[Device]:
    """port.vlans, vlan.set and vlan.delete on a bridge that filters VLANs
    (DSA): the `config bridge-vlan` sections of /etc/config/network, applied
    with rollback, checked in netifd's bridge VLANs, then confirmed."""
    value = change.value if isinstance(change.value, dict) else {}
    c = client_from(cfg)
    try:
        c.login()
        devices = Reader(c).required("network.device", "status")
        network = read_configs(c, ["network"])["network"]
        if vl.sections(network, "switch"):
            raise PluginError(
                "this router's switch uses swconfig (an older driver): its VLANs are not "
                "changed by the plugin"
            )
        if change.kind == "port.vlans":
            port = change.port or ""
            check_port(devices, network, port)
            untagged, tagged = vl.wanted(change.value)
            ops = vl.plan_port(network, port, untagged, tagged)
            what = f"the VLANs of {port}"

            def done() -> bool:
                status = c.call("network.device", "status")
                got = bridge_vlans(status if isinstance(status, dict) else {})[0].get(port)
                return tags(got) == (untagged, tagged)

        elif change.kind == "vlan.set":
            vid, name = vl.vlan_id(value, "id", change.kind), str(value.get("name") or "").strip()
            bridge, ops = vl.plan_set(network, vid, name)
            what = f"VLAN {vid}"

            def done() -> bool:
                saved = vl.names(c.call("uci", "get", {"config": "network"}))
                return (bridge, vid) in running_vlans(c) and saved.get(vid, "") == name

        else:
            vid = vl.vlan_id(value, "id", change.kind)
            ops = vl.plan_delete(network, vid)
            what = f"VLAN {vid}"

            def done() -> bool:
                return all(v != vid for _, v in running_vlans(c))

        if ops:
            commit(c, ["network"], ops, done, what)
    finally:
        c.close()
    return collect(cfg)


def apply_networks(cfg: Config, change: Change) -> list[Device]:
    """network.set / network.delete (the interface, its VLAN, the dhcp section
    and the firewall zone, rules and forwarding) and reservation.set /
    reservation.delete (`config host` in /etc/config/dhcp). Staged in this
    session, applied with rollback, read again, then confirmed."""
    value = change.value if isinstance(change.value, dict) else {}
    c = client_from(cfg)
    try:
        c.login()
        reservation = change.kind.startswith("reservation.")
        names = ["dhcp"] if reservation else ["network", "dhcp", "firewall"]
        configs = read_configs(c, names)
        if reservation:
            mac = nets.norm_mac(value.get("mac"))
            if change.kind == "reservation.set":
                ops = nets.plan_reserve(configs["dhcp"], value)
                ip = str(value.get("ip") or "")

                def done() -> bool:
                    found = nets.reservations(read_configs(c, ["dhcp"])["dhcp"])
                    return any(r["mac"] == mac and r["ip"] == ip for r in found)

            else:
                ops = nets.plan_unreserve(configs["dhcp"], mac)

                def done() -> bool:
                    found = nets.reservations(read_configs(c, ["dhcp"])["dhcp"])
                    return all(r["mac"] != mac for r in found)

            what = f"the reservation of {mac}"
        else:
            wans = wan_names(Reader(c).required("network.interface", "dump"), configs["firewall"])
            vid = vl.vlan_id(value, "vlan", change.kind)
            what = f"the network of VLAN {vid}"
            if change.kind == "network.set":
                ops = nets.plan_set(configs, value, wans, urlparse(c.base).hostname)

                def done() -> bool:
                    return nets.matches(read_configs(c, names), value, wans)

            else:
                ops = nets.plan_delete(configs, vid, wans)

                def done() -> bool:
                    got = read_configs(c, names)
                    found = nets.networks(got["network"], got["dhcp"], got["firewall"], wans)
                    return all(n["vlan"] != vid for n in found)

        if ops:
            commit(c, names, ops, done, what)
    finally:
        c.close()
    return collect(cfg)


# --- DNS names, pause and Wake-on-LAN ------------------------------------------


def own_ips(dump: Any) -> set[str]:
    """The router's own IPv4 addresses (never paused)."""
    logical = dump.get("interface") if isinstance(dump, dict) else None
    out = set()
    for li in logical or []:
        for a in (li.get("ipv4-address") or []) if isinstance(li, dict) else []:
            if isinstance(a, dict) and isinstance(a.get("address"), str):
                out.add(a["address"])
    return out


def apply_services(cfg: Config, change: Change) -> list[Device]:
    """dns.set / dns.delete (`config domain` in /etc/config/dhcp) and
    pause.set / pause.delete (the "Omini pause" rules by MAC in
    /etc/config/firewall). Staged in this session, applied with
    rollback, read again, then confirmed."""
    value = change.value if isinstance(change.value, dict) else {}
    c = client_from(cfg)
    try:
        c.login()
        if change.kind.startswith("dns."):
            names = ["dhcp"]
            dhcp = read_configs(c, names)["dhcp"]
            name = svc.dns_name(value.get("name"))
            if change.kind == "dns.set":
                ops = svc.plan_dns_set(dhcp, value)
                want = svc.ipv4(value.get("ip"))
            else:
                ops = svc.plan_dns_delete(dhcp, value)
                want = None
            what = f"the DNS name {name}"

            def done() -> bool:
                return svc.has_dns(read_configs(c, names)["dhcp"], name, want)

        else:
            names = ["firewall"]
            firewall = read_configs(c, names)["firewall"]
            dump = Reader(c).required("network.interface", "dump")
            wans = wan_names(dump, firewall)
            mac = nets.norm_mac(value.get("mac"))
            if change.kind == "pause.set":
                protected = own_ips(dump) | {urlparse(c.base).hostname or ""}
                ops = svc.plan_pause_set(firewall, value, wans, protected)
                ips: list[str] | None = [
                    ip for ip in (svc.ipv4(x) for x in value.get("ips") or []) if ip
                ]
                what = f"the pause of {mac}"
            else:
                ops = svc.plan_pause_delete(firewall, value, wans)
                ips = None
                what = f"the resume of {mac}"

            def done() -> bool:
                got = read_configs(c, names)["firewall"]
                return svc.pause_matches(got, mac, ips, wans)

        if ops:
            commit(c, names, ops, done, what)
    finally:
        c.close()
    return collect(cfg)


ETHERWAKE = "/usr/bin/etherwake"
WOL_ACL = (
    '"ubus": {"file": ["stat", "exec"]} and "file": {"/usr/bin/etherwake": ["list", "exec"]} '
    "in its ACL file (see the plugin's README)"
)


def wol_device(dump: Any, ip: str | None) -> str:
    """The device (br-lan, br-lan.30...) of the LAN-side interface whose
    network holds ip; without ip, the `lan` interface's."""
    wans = wan_names(dump)
    logical = dump.get("interface") if isinstance(dump, dict) else None
    for li in logical or []:
        if not isinstance(li, dict):
            continue
        name = str(li.get("interface") or "")
        dev = li.get("l3_device") or li.get("device")
        if not isinstance(dev, str) or name in wans or dev in wans or name == "loopback":
            continue
        if ip is None:
            if name == "lan":
                return dev
            continue
        for a in li.get("ipv4-address") or []:
            try:
                net = ipaddress.ip_interface(f"{a.get('address')}/{a.get('mask')}").network
            except (ValueError, AttributeError):
                continue
            if ipaddress.ip_address(ip) in net:
                return dev
    if ip is None:
        raise PluginError("the router has no lan interface to send the Wake-on-LAN packet on")
    raise PluginError(f"none of the router's local networks holds {ip}")


def apply_wol(cfg: Config, change: Change) -> list[Device]:
    """wol.send: etherwake (`file exec` of /usr/bin/etherwake only, as the ACL
    allows) sends the magic packet, broadcast, on the interface of the
    network that holds the device's address. Nothing to verify."""
    value = change.value if isinstance(change.value, dict) else {}
    mac = norm_mac(value.get("mac"))
    if not mac:
        raise PluginError("Wake-on-LAN needs the device's MAC address")
    ip = svc.ipv4(value.get("ip")) if value.get("ip") else None
    if value.get("ip") and ip is None:
        raise PluginError(f"{value.get('ip')!r} is not an IPv4 address")
    c = client_from(cfg)
    try:
        c.login()
        dev = wol_device(Reader(c).required("network.interface", "dump"), ip)
        try:
            c.call("file", "stat", {"path": ETHERWAKE})
        except AccessDenied:
            pass  # the ACL may grant exec only: the exec below says what is missing
        except Unavailable as e:
            raise PluginError(
                "etherwake is not installed on the router: install it (opkg install etherwake, "
                "or apk add etherwake) to wake devices from it"
            ) from e
        try:
            res = c.call("file", "exec", {"command": ETHERWAKE, "params": ["-b", "-i", dev, mac]})
        except AccessDenied as e:
            raise PluginError(
                f"the ACL of user {c.username!r} does not allow running etherwake: it needs "
                + WOL_ACL
            ) from e
        except Unavailable as e:
            raise PluginError(f"etherwake did not run on the router: {e}") from e
        code = res.get("code") if isinstance(res, dict) else None
        if code not in (0, None):
            err = str(res.get("stderr") or "").strip() if isinstance(res, dict) else ""
            raise PluginError(f"etherwake failed (exit {code}){': ' + err if err else ''}")
    finally:
        c.close()
    return collect(cfg)
