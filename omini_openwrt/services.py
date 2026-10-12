"""Local DNS names and paused devices on an OpenWrt router.

- DNS: dnsmasq's `config domain` sections in /etc/config/dhcp (`name`, `ip`:
  dnsmasq answers that name, and names under it, with the address); the local
  domain is the dnsmasq section's `domain` option.
- Pause: an fw4 ipset `omini_paused` in /etc/config/firewall (`list entry`
  with the paused IPv4 addresses, `list omini` with "<mac> <ip>,<ip> <name>"
  so the MAC behind each address is kept) and, on every zone but the WAN's, a
  rule "Omini pause" rejecting that set towards the WAN zone (the internet;
  the device's own and other local networks stay reachable).

Everything here plans uci calls (or reads); collect.apply stages, applies
(with rollback), verifies and confirms them."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from omini_sdk import PluginError

from omini_openwrt.networks import add, delete, norm_mac, setv, wan_zone
from omini_openwrt.vlans import Op, as_list
from omini_openwrt.wifi import sections

PAUSE_SET = "omini_paused"
PAUSE_RULE = "Omini pause"
# The ipset's option keeping which MAC each address belongs to (fw4 ignores it).
PAUSE_MAP = "omini"
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)*$"
)


# --- DNS ----------------------------------------------------------------------


def dns_domain(dhcp: Any) -> str | None:
    """The local domain dnsmasq hands out (`option domain 'lan'`)."""
    for s in sections(dhcp, "dnsmasq"):
        domain = str(s.get("domain") or "").strip().strip(".").lower()
        if domain:
            return domain
    return None


def dns_name(value: Any) -> str:
    return str(value or "").strip().rstrip(".").lower()


def ipv4(value: Any) -> str | None:
    try:
        return str(ipaddress.IPv4Address(str(value or "").strip()))
    except ValueError:
        return None


def dns_records(dhcp: Any) -> list[dict[str, str]]:
    """Every `config domain` with a name and an IPv4 address (A records only)."""
    out = []
    for s in sections(dhcp, "domain"):
        name, ip = dns_name(s.get("name")), ipv4(s.get("ip"))
        if name and ip and {"name": name, "ip": ip} not in out:
            out.append({"name": name, "ip": ip})
    return sorted(out, key=lambda r: (r["name"], r["ip"]))


def _named(dhcp: Any, name: str) -> list[dict[str, Any]]:
    return [s for s in sections(dhcp, "domain") if dns_name(s.get("name")) == name]


def plan_dns_set(dhcp: Any, value: dict[str, Any]) -> list[Op]:
    name, ip = dns_name(value.get("name")), ipv4(value.get("ip"))
    if not name or not HOSTNAME.match(name):
        raise PluginError(f"{value.get('name')!r} is not a DNS name")
    if not ip:
        raise PluginError(f"a DNS name needs an IPv4 address, not {value.get('ip')!r}")
    found = _named(dhcp, name)
    if not found:
        return [add("dhcp", "domain", {"name": name, "ip": ip})]
    ops: list[Op] = []
    first, rest = found[0], found[1:]
    if ipv4(first.get("ip")) != ip or str(first.get("name")) != name:
        ops.append(setv("dhcp", first[".name"], {"name": name, "ip": ip}))
    # One answer per name: duplicates of it would make dnsmasq answer both.
    ops += [delete("dhcp", s[".name"]) for s in rest]
    return ops


def plan_dns_delete(dhcp: Any, value: dict[str, Any]) -> list[Op]:
    name = dns_name(value.get("name"))
    if not name:
        raise PluginError("dns.delete needs the name")
    return [delete("dhcp", s[".name"]) for s in _named(dhcp, name)]


def has_dns(dhcp: Any, name: str, ip: str | None) -> bool:
    found = [r for r in dns_records(dhcp) if r["name"] == name]
    return [r["ip"] for r in found] == [ip] if ip else not found


# --- pause --------------------------------------------------------------------


def pause_section(firewall: Any) -> dict[str, Any] | None:
    return next((s for s in sections(firewall, "ipset") if s.get("name") == PAUSE_SET), None)


def pause_rules(firewall: Any) -> list[dict[str, Any]]:
    return [
        r
        for r in sections(firewall, "rule")
        if r.get("name") == PAUSE_RULE and r.get("ipset") == PAUSE_SET
    ]


def paused(firewall: Any) -> list[dict[str, Any]] | None:
    """The paused devices, from the ipset's "<mac> <ips> <name>" entries; None
    without the ipset. Only addresses the set really holds are reported."""
    s = pause_section(firewall)
    if s is None:
        return None
    held = set(as_list(s.get("entry")))
    out = []
    for line in as_list_lines(s.get(PAUSE_MAP)):
        parts = line.split(" ", 2)
        mac = norm_mac(parts[0])
        if not mac:
            continue
        ips = [ip for ip in (parts[1].split(",") if len(parts) > 1 else []) if ip in held]
        name = parts[2].strip() if len(parts) > 2 else ""
        out.append({"mac": mac, "ips": ips or None, "name": name or None})
    return sorted(out, key=lambda p: p["mac"])


def as_list_lines(value: Any) -> list[str]:
    """A uci list whose items contain spaces (never split them)."""
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)] if value else []


def lan_zones(firewall: Any, wans: set[str]) -> list[str]:
    wan = wan_zone(firewall, wans)
    return sorted({str(z.get("name")) for z in sections(firewall, "zone") if z.get("name")} - {wan})


def _pause_writes(
    firewall: Any, devices: dict[str, tuple[list[str], str]], wans: set[str]
) -> list[Op]:
    """The ipset and the rules for exactly `devices` (mac → (ips, name)); with
    none, both are removed."""
    s = pause_section(firewall)
    rules = pause_rules(firewall)
    if not devices:
        ops = [delete("firewall", r[".name"]) for r in rules]
        if s is not None:
            ops.append(delete("firewall", s[".name"]))
        return ops
    entries = sorted({ip for ips, _ in devices.values() for ip in ips}, key=ipaddress.IPv4Address)
    lines = [
        " ".join(x for x in (mac, ",".join(ips), name) if x)
        for mac, (ips, name) in sorted(devices.items())
    ]
    values = {"entry": entries, PAUSE_MAP: lines}
    ops: list[Op] = []
    if s is None:
        ops.append(
            add(
                "firewall",
                "ipset",
                {"name": PAUSE_SET, "family": "ipv4", "match": ["src_ip"], **values},
            )
        )
    else:
        ops.append(setv("firewall", s[".name"], values))
    wan = wan_zone(firewall, wans)
    have = {str(r.get("src")) for r in rules}
    for zone in lan_zones(firewall, wans):
        if zone not in have:
            ops.append(
                add(
                    "firewall",
                    "rule",
                    {
                        "name": PAUSE_RULE,
                        "src": zone,
                        "dest": wan,
                        "ipset": PAUSE_SET,
                        "proto": "all",
                        "family": "ipv4",
                        "target": "REJECT",
                    },
                )
            )
    return ops


def _current(firewall: Any) -> dict[str, tuple[list[str], str]]:
    return {p["mac"]: (p["ips"] or [], p["name"] or "") for p in paused(firewall) or []}


def plan_pause_set(
    firewall: Any, value: dict[str, Any], wans: set[str], protected: set[str]
) -> list[Op]:
    mac = norm_mac(value.get("mac"))
    if not mac:
        raise PluginError("pausing needs the device's MAC address")
    ips = []
    for raw in value.get("ips") or []:
        ip = ipv4(raw)
        if ip and ip not in ips:
            ips.append(ip)
    if not ips:
        raise PluginError("pausing needs the device's IPv4 address (only IPv4 is blocked)")
    mine = sorted(set(ips) & protected)
    if mine:
        raise PluginError(f"{', '.join(mine)} is the router itself: it cannot be paused")
    if not lan_zones(firewall, wans):
        raise PluginError("the router's firewall has no zone besides the WAN's")
    name = re.sub(r"\s+", " ", str(value.get("name") or "")).strip()
    devices = _current(firewall)
    # An address the device took from another paused one moves with it.
    for other, (other_ips, other_name) in list(devices.items()):
        devices[other] = ([ip for ip in other_ips if ip not in ips], other_name)
    devices[mac] = (ips, name)
    if devices == _current(firewall) and pause_rules(firewall):
        return []
    return _pause_writes(firewall, devices, wans)


def plan_pause_delete(firewall: Any, value: dict[str, Any], wans: set[str]) -> list[Op]:
    mac = norm_mac(value.get("mac"))
    if not mac:
        raise PluginError("resuming needs the device's MAC address")
    devices = _current(firewall)
    if mac not in devices:
        return []
    del devices[mac]
    return _pause_writes(firewall, devices, wans)


def pause_matches(firewall: Any, mac: str, ips: list[str] | None, wans: set[str]) -> bool:
    """Whether the firewall config pauses mac on ips (None: mac not paused)."""
    found = {p["mac"]: p["ips"] or [] for p in paused(firewall) or []}
    if ips is None:
        return mac not in found
    zones = {str(r.get("src")) for r in pause_rules(firewall)}
    return sorted(found.get(mac, [])) == sorted(ips) and set(lan_zones(firewall, wans)) <= zones
