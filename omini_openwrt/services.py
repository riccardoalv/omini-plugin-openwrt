"""Local DNS names and paused devices on an OpenWrt router.

- DNS: dnsmasq's `config domain` sections in /etc/config/dhcp (`name`, `ip`:
  dnsmasq answers that name, and names under it, with the address); the local
  domain is the dnsmasq section's `domain` option.
- Pause: by MAC, so both IPv4 and IPv6 (whatever addresses the device takes,
  IPv6 temporary ones included): on every zone but the WAN's, one fw4 rule
  "Omini pause" (`list src_mac` with every paused MAC, `family any`, `proto
  all`, target REJECT) towards the WAN zone. Going to the WAN zone is the
  internet, so the device's own and other local networks (IPv4 and IPv6)
  stay reachable. Each rule also keeps `list omini` "<mac> <ip>,<ip> <name>"
  (an option fw4 ignores) so collect reports the addresses Omini paused. The
  0.3.0 shape (an ipset `omini_paused` of IPv4 addresses and rules matching
  it) is still read, and replaced on the next pause or resume.

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

PAUSE_SET = "omini_paused"  # the 0.3.0 ipset (migrated)
PAUSE_RULE = "Omini pause"
# The rules' option keeping each paused MAC's addresses and name (fw4 ignores it).
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


def old_set(firewall: Any) -> dict[str, Any] | None:
    """The ipset of version 0.3.0 (IPv4 addresses only), migrated on the next change."""
    return next((s for s in sections(firewall, "ipset") if s.get("name") == PAUSE_SET), None)


def old_rules(firewall: Any) -> list[dict[str, Any]]:
    return [
        r
        for r in sections(firewall, "rule")
        if r.get("name") == PAUSE_RULE and r.get("ipset") == PAUSE_SET
    ]


def pause_rules(firewall: Any) -> list[dict[str, Any]]:
    """Omini's rules by MAC (one per LAN zone)."""
    return [
        r for r in sections(firewall, "rule") if r.get("name") == PAUSE_RULE and not r.get("ipset")
    ]


def parse_line(line: str) -> tuple[str, list[str], str] | None:
    """'<mac> <ip>,<ip> <name>' → (mac, ips, name)."""
    parts = line.split(" ", 2)
    mac = norm_mac(parts[0])
    if not mac:
        return None
    ips = [ip for ip in (parts[1].split(",") if len(parts) > 1 else []) if ip]
    return mac, ips, parts[2].strip() if len(parts) > 2 else ""


def paused(firewall: Any) -> list[dict[str, Any]] | None:
    """The paused devices: the MACs the "Omini pause" rules match, with the
    addresses and name their `omini` lines keep; and those of a 0.3.0 ipset
    (its addresses that the set really holds). None without either."""
    rules, s = pause_rules(firewall), old_set(firewall)
    if not rules and s is None:
        return None
    found: dict[str, dict[str, Any]] = {}
    for r in rules:
        macs = {norm_mac(m) for m in as_list(r.get("src_mac"))}
        for line in as_list_lines(r.get(PAUSE_MAP)):
            parsed = parse_line(line)
            if parsed and parsed[0] in macs:
                found.setdefault(parsed[0], {"mac": parsed[0], "ips": parsed[1], "name": parsed[2]})
    if s is not None:
        held = set(as_list(s.get("entry")))
        for line in as_list_lines(s.get(PAUSE_MAP)):
            parsed = parse_line(line)
            if parsed:
                ips = [ip for ip in parsed[1] if ip in held]
                found.setdefault(parsed[0], {"mac": parsed[0], "ips": ips, "name": parsed[2]})
    return [
        {"mac": p["mac"], "ips": p["ips"] or None, "name": p["name"] or None}
        for p in sorted(found.values(), key=lambda p: p["mac"])
    ]


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
    """The rules for exactly `devices` (mac → (ips, name)); with none, they
    are removed. A 0.3.0 ipset and its rules always go (migrated)."""
    ops = [delete("firewall", r[".name"]) for r in old_rules(firewall)]
    s = old_set(firewall)
    if s is not None:
        ops.append(delete("firewall", s[".name"]))
    rules = pause_rules(firewall)
    if not devices:
        return ops + [delete("firewall", r[".name"]) for r in rules]
    macs = sorted(devices)
    lines = [
        " ".join(x for x in (mac, ",".join(ips), name) if x)
        for mac, (ips, name) in sorted(devices.items())
    ]
    wan = wan_zone(firewall, wans)
    values = {
        "dest": wan,
        "src_mac": macs,
        "proto": "all",
        "family": "any",
        "target": "REJECT",
        PAUSE_MAP: lines,
    }
    zones = lan_zones(firewall, wans)
    have: set[str] = set()
    for r in rules:
        zone = str(r.get("src"))
        if zone in zones and zone not in have:
            have.add(zone)
            ops.append(setv("firewall", r[".name"], values))
        else:  # a zone that is gone, or a duplicate
            ops.append(delete("firewall", r[".name"]))
    for zone in zones:
        if zone not in have:
            ops.append(add("firewall", "rule", {"name": PAUSE_RULE, "src": zone, **values}))
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
        raise PluginError("pausing needs the device's IPv4 address")
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
    if devices == _current(firewall) and pause_matches(firewall, mac, ips, wans):
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
    """Whether the firewall config pauses mac on ips by MAC, from every LAN
    zone to the WAN's, in both families (None: mac not paused at all)."""
    found = {p["mac"]: p["ips"] or [] for p in paused(firewall) or []}
    if ips is None:
        return mac not in found
    wan = wan_zone(firewall, wans)
    zones = {
        str(r.get("src"))
        for r in pause_rules(firewall)
        if mac in {norm_mac(m) for m in as_list(r.get("src_mac"))}
        and str(r.get("dest")) == wan
        and str(r.get("family") or "any") == "any"
        and str(r.get("target") or "").upper() == "REJECT"
        and str(r.get("enabled", "1")) != "0"
    }
    return (
        sorted(found.get(mac, [])) == sorted(ips)
        and set(lan_zones(firewall, wans)) <= zones
        and old_set(firewall) is None
    )
