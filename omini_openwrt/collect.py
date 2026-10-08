"""Turns ubus answers of an OpenWrt router or access point into an Omini device."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from omini_sdk import (
    ArpEntry,
    Config,
    Device,
    DhcpLease,
    Gateway,
    Host,
    Interface,
    PluginError,
    PortVlans,
    Storage,
    Temperature,
    Vlan,
    WirelessClient,
    log,
)

from omini_openwrt.client import AccessDenied, Client, UbusError

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


def vlan_of(name: str) -> tuple[str | None, int | None]:
    base, _, vid = name.rpartition(".")
    if base and vid.isdigit() and 1 <= int(vid) <= 4094:
        return base, int(vid)
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
        routes = [r for r in li.get("route") or [] if isinstance(r, dict) and is_default_route(r)]
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

    interfaces: list[Interface] = []
    seen = set()
    for name in sorted(devices):
        st = devices[name]
        if name == "lo" or st.get("present") is False:
            continue
        seen.add(name)
        kind = iface_type(name, st)
        speed, duplex = parse_speed(st.get("speed"))
        up = st.get("carrier") if isinstance(st.get("carrier"), bool) else st.get("up")
        stats = st.get("statistics") if isinstance(st.get("statistics"), dict) else {}
        parent, vid = vlan_of(name) if kind == "vlan" else (None, None)
        members = st.get("bridge-members")
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
                speed_mbps=speed if up is not False else None,
                duplex=duplex if up is not False else None,
                rx_bytes=counter(stats.get("rx_bytes")),
                tx_bytes=counter(stats.get("tx_bytes")),
                rx_errors=counter(stats.get("rx_errors")),
                tx_errors=counter(stats.get("tx_errors")),
                vlan=vid,
                vlans=port_vlans.get(name),
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
    for bridge, vid in sorted(set(vlan_list), key=lambda v: (v[1], v[0])):
        dev = f"{bridge}.{vid}"
        vlans.append(
            Vlan(
                id=vid,
                name=", ".join(names_on.get(dev, [])) or None,
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
    key = lan_mac or (macs[0] if macs else None) or host or str(board.get("hostname"))

    release = board.get("release") if isinstance(board.get("release"), dict) else {}
    os_version = " ".join(
        str(release[k]) for k in ("distribution", "version") if release.get(k)
    ).strip()

    mem = info.get("memory") if isinstance(info.get("memory"), dict) else {}
    swap = info.get("swap") if isinstance(info.get("swap"), dict) else {}
    load = [round(v / 65536, 2) for v in info.get("load") or [] if isinstance(v, int)][:3]

    leases, hosts = map_leases_and_hosts(raw["leases"], raw["hints"])
    wireless = map_wireless(raw["wifi"])

    return Device(
        key=key,
        name=str(board.get("hostname") or host or "OpenWrt"),
        host=host,
        role="router" if any(g for g in gateways) else "ap",
        model=board.get("model") or None,
        os_version=os_version or None,
        uptime_s=counter(info.get("uptime")),
        mem_pct=mem_pct(mem),
        swap_pct=pct_used(swap.get("total"), swap.get("free")),
        load_avg=load or None,
        temperatures=map_temperatures(raw["temps"]) or None,
        storage=map_storage(info) or None,
        macs=macs or None,
        ips=list(dict.fromkeys(device_ips)) or None,
        interfaces=interfaces or None,
        arp=parse_arp(raw["arp"]) if raw["arp"] else None,
        dhcp_leases=leases,
        wireless_clients=wireless,
        gateways=gateways or None,
        hosts=hosts,
        vlans=vlans or None,
    )


def pct_used(total: Any, free: Any) -> float | None:
    t, f = as_int(total), as_int(free)
    if not t or t <= 0 or f is None:
        return None
    return round(min(100.0, max(0.0, (t - f) * 100 / t)), 1)


def mem_pct(mem: dict[str, Any]) -> float | None:
    available = as_int(mem.get("available"))
    if available is None:
        parts = [as_int(mem.get(k)) for k in ("free", "buffered", "cached")]
        if parts[0] is None:
            return None
        available = sum(p or 0 for p in parts)
    return pct_used(mem.get("total"), available)


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
