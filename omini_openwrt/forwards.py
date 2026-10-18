"""Ports an OpenWrt router forwards from the internet (read only).

- By hand: fw4's `config redirect` sections in /etc/config/firewall with
  target DNAT (the default target of a redirect): `src_dport` (a port or a
  range, "8000-8010" or "8000:8010"), `dest_ip`, `dest_port` (the same port
  when missing), `proto` ("tcp udp" when missing), `src_ip`, `name`,
  `enabled`.
- By devices (UPnP IGD / NAT-PMP, the miniupnpd package): luci-app-upnp's
  `luci.upnp get_status` (`rules`: proto, extport, intaddr, intport,
  description), else miniupnpd's lease file, one line per mapping:
  `PROTO:EXT_PORT:INT_IP:INT_PORT:TIMESTAMP:DESCRIPTION` (older releases
  have no timestamp).

Omini never changes them."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from omini_openwrt.wifi import sections

OFF = {"0", "false", "off", "no", "disabled"}
RANGE = re.compile(r"^\s*(\d{1,5})\s*(?:[-:]\s*(\d{1,5}))?\s*$")

# miniupnpd's lease file (option upnp_lease_file of /etc/config/upnpd).
LEASE_FILE = "/var/run/miniupnpd.leases"


def port_range(value: Any) -> tuple[int, int | None] | None:
    """A port or a range: (first, last or None); None when it is no port."""
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else None
    m = RANGE.match(str(value)) if isinstance(value, (str, int)) else None
    if not m:
        return None
    first, last = int(m.group(1)), int(m.group(2)) if m.group(2) else None
    if not 1 <= first <= 65535 or (last is not None and not first <= last <= 65535):
        return None
    return first, last if last != first else None


def protocol(value: Any) -> str | None:
    """fw4's proto (a word list, or a uci list) → tcp, udp or tcp/udp; None for
    anything else (icmp, all...), which forwards no port."""
    if value is None:
        return "tcp/udp"
    words = value if isinstance(value, list) else str(value).split()
    found = {str(w).strip().lower() for w in words}
    if "tcpudp" in found:
        found |= {"tcp", "udp"}
    found.discard("tcpudp")
    if found == {"tcp", "udp"}:
        return "tcp/udp"
    if found in ({"tcp"}, {"udp"}):
        return found.pop()
    return None


def address(value: Any) -> str | None:
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except ValueError:
        return None


def source(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else None
    try:
        return str(ipaddress.ip_network(str(value or "").strip(), strict=False))
    except ValueError:
        return None


def redirects(firewall: Any) -> list[dict[str, Any]]:
    """The DNAT redirects of the firewall config, as PortForward fields."""
    out = []
    for s in sections(firewall, "redirect"):
        if str(s.get("target") or "DNAT").upper() != "DNAT":
            continue
        proto = protocol(s.get("proto"))
        ext = port_range(s.get("src_dport"))
        if proto is None or ext is None:
            continue
        internal = port_range(s.get("dest_port")) if s.get("dest_port") is not None else ext
        src = s.get("src_ip")
        name = s.get("name")
        out.append(
            {
                "protocol": proto,
                "external_port": ext[0],
                "external_port_end": ext[1],
                "internal_ip": address(s.get("dest_ip")),
                "internal_port": internal[0] if internal else None,
                "source": source(src) if src else None,
                "description": str(name) if isinstance(name, str) and name else None,
                "enabled": str(s.get("enabled", "1")).strip().lower() not in OFF,
                "upnp": False,
            }
        )
    return out


def upnp_protocol(value: Any) -> str | None:
    p = str(value or "").strip().lower()
    return p if p in ("tcp", "udp") else None


def upnp_entry(proto: Any, ext: Any, ip: Any, internal: Any, desc: Any) -> dict[str, Any] | None:
    p, e, i = upnp_protocol(proto), port_range(ext), port_range(internal)
    if p is None or e is None:
        return None
    return {
        "protocol": p,
        "external_port": e[0],
        "internal_ip": address(ip),
        "internal_port": i[0] if i else None,
        "description": (str(desc).strip() or None) if desc else None,
        "enabled": True,
        "upnp": True,
    }


def upnp_status(status: Any) -> list[dict[str, Any]] | None:
    """luci.upnp get_status's `rules`; None when it has none to say."""
    rules = status.get("rules") if isinstance(status, dict) else None
    if not isinstance(rules, list):
        return None
    out = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        entry = upnp_entry(
            r.get("proto"),
            r.get("extport"),
            r.get("intaddr"),
            r.get("intport"),
            r.get("description"),
        )
        if entry:
            out.append(entry)
    return out


def upnp_leases(text: str) -> list[dict[str, Any]]:
    """miniupnpd's lease file."""
    out = []
    for line in text.splitlines():
        parts = line.strip().split(":", 5)
        if len(parts) < 4:
            continue
        # A timestamp (miniupnpd 2.x) comes before the description.
        stamped = len(parts) == 6 and parts[4].strip().isdigit()
        desc = parts[5] if stamped else ":".join(parts[4:])
        entry = upnp_entry(parts[0], parts[1], parts[2], parts[3], desc)
        if entry:
            out.append(entry)
    return out
