"""A VLAN's network on an OpenWrt router: its interface and gateway
(/etc/config/network), its DHCP range (/etc/config/dhcp, dnsmasq) and its
access (/etc/config/firewall, a zone of its own). Read every round, changed by
network.set and network.delete; the DHCP's fixed addresses (`config host`) by
reservation.set and reservation.delete.

Access presets, on the network's own firewall zone:
- internet: input REJECT, rules that let DNS and DHCP reach the router, a
  forwarding to the WAN's zone and a rule rejecting the private ranges there;
- all: input ACCEPT and a rule accepting everything to every zone (`dest '*'`);
- local: input REJECT and the DNS and DHCP rules only (no forwarding).
Anything else reads as custom. Rules Omini writes are named "Omini: ...".

Everything here plans uci calls (or reads); collect.apply stages, applies
(with rollback), verifies and confirms them."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from omini_sdk import PluginError

from omini_openwrt.vlans import Op, as_list, bridge_of, bridge_vlans, bridges, filtering_off
from omini_openwrt.wifi import interface_for, sections, vlan_of_interface

PRIVATE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
PREFIX = "Omini: "
ON = {"1", "true", "on", "yes", "enabled"}
DROP = {"REJECT", "DROP"}
# OpenWrt's defaults for a `config dhcp` section without start or limit.
DEFAULT_START, DEFAULT_LIMIT = 100, 150


def by_name(config: Any, name: str) -> dict[str, Any] | None:
    values = config.get("values") if isinstance(config, dict) else None
    s = values.get(name) if isinstance(values, dict) else None
    return s if isinstance(s, dict) else None


def enabled(section: dict[str, Any]) -> bool:
    return str(section.get("enabled", "1")).strip().lower() not in ("0", "false", "off", "no")


# --- reading ------------------------------------------------------------------


def gateway_of(iface: dict[str, Any]) -> str | None:
    """The interface's first IPv4 address with its prefix: `ipaddr` as
    192.168.30.1/24, or with `netmask`."""
    if iface.get("proto") != "static":
        return None
    for addr in as_list(iface.get("ipaddr")):
        try:
            if "/" in addr:
                return str(ipaddress.IPv4Interface(addr))
            mask = str(iface.get("netmask") or "255.255.255.0")
            return str(ipaddress.IPv4Interface(f"{addr}/{mask}"))
        except ValueError:
            continue
    return None


def dhcp_section(dhcp: Any, ifname: str) -> dict[str, Any] | None:
    found = [s for s in sections(dhcp, "dhcp") if s.get("interface") == ifname]
    return found[0] if found else None


def dhcp_of(dhcp: Any, ifname: str, gateway: str) -> dict[str, Any]:
    s = dhcp_section(dhcp, ifname)
    if s is None or str(s.get("ignore", "0")).lower() in ON:
        return {"enabled": False}
    net = ipaddress.IPv4Interface(gateway).network
    try:
        start = int(str(s.get("start") or DEFAULT_START))
        limit = int(str(s.get("limit") or DEFAULT_LIMIT))
    except ValueError:
        return {"enabled": True}
    first = net.network_address + start
    last = min(int(first) + limit - 1, int(net.broadcast_address) - 1)
    if first not in net or limit < 1:
        return {"enabled": True}
    return {"enabled": True, "from": str(first), "to": str(ipaddress.IPv4Address(last))}


def zone_of(firewall: Any, ifname: str) -> dict[str, Any] | None:
    return next(
        (z for z in sections(firewall, "zone") if ifname in as_list(z.get("network"))), None
    )


def wan_zone(firewall: Any, wans: set[str]) -> str:
    """The WAN's zone: the one holding a WAN interface, else the masquerading one."""
    for z in sections(firewall, "zone"):
        if set(as_list(z.get("network"))) & wans:
            return str(z.get("name"))
    for z in sections(firewall, "zone"):
        if str(z.get("masq", "0")) in ON:
            return str(z.get("name"))
    return "wan"


def rule_shape(r: dict[str, Any], wan: str) -> str:
    """A rule as one of the presets' pieces: dns, dhcp, block_private, pass_any or other."""
    target = str(r.get("target") or "").upper()
    dest, port = str(r.get("dest") or ""), str(r.get("dest_port") or "")
    if r.get("src_ip") or r.get("src_port") or r.get("src_mac"):
        return "other"
    if target == "ACCEPT" and not dest and port in ("53", "domain"):
        return "dns"
    if target == "ACCEPT" and not dest and port in ("67", "67-68", "67:68"):
        return "dhcp"
    if target in DROP and dest == wan and sorted(as_list(r.get("dest_ip"))) == sorted(PRIVATE):
        return "block_private"
    if target == "ACCEPT" and dest == "*" and not port and not r.get("dest_ip"):
        return "pass_any"
    return "other"


def access_of(firewall: Any, ifname: str, wans: set[str]) -> str:
    """Which of the three presets the network's zone is, else custom."""
    z = zone_of(firewall, ifname)
    if z is None or as_list(z.get("network")) != [ifname]:
        return "custom"
    name, wan = str(z.get("name")), wan_zone(firewall, wans)
    rules = [r for r in sections(firewall, "rule") if r.get("src") == name and enabled(r)]
    fwds = {
        str(f.get("dest"))
        for f in sections(firewall, "forwarding")
        if f.get("src") == name and enabled(f)
    }
    shapes = {rule_shape(r, wan) for r in rules}
    if "other" in shapes:
        return "custom"
    core = shapes - {"dns", "dhcp"}
    policy = str(z.get("input") or "").upper()
    if policy == "ACCEPT" and core == {"pass_any"}:
        return "all"
    if policy in DROP and "dns" in shapes:
        if fwds == {wan} and core == {"block_private"}:
            return "internet"
        if not fwds and not core:
            return "local"
    return "custom"


def parent_of(network: Any, device: str) -> str | None:
    """The bridge (br-lan.30) or port (an 8021q device's ifname) a VLAN rides on."""
    for d in sections(network, "device"):
        if d.get("name") == device and d.get("type") in ("8021q", "8021ad"):
            return str(d.get("ifname") or "") or None
    base = re.sub(r"\.\d+$", "", device)
    return base if base != device or base in bridges(network) else None


def networks(network: Any, dhcp: Any, firewall: Any, wans: set[str]) -> list[dict[str, Any]]:
    """Each VLAN's network: the statically addressed, private, LAN-side
    interfaces whose VLAN is known (the LAN is VLAN 1)."""
    out = []
    for s in sections(network, "interface"):
        ifname = str(s.get(".name") or "")
        device = str(s.get("device") or "")
        if not ifname or ifname == "loopback" or ifname in wans or device in wans:
            continue
        gateway = gateway_of(s)
        vid = vlan_of_interface(network, ifname)
        if not gateway or not vid:
            continue
        if not ipaddress.IPv4Interface(gateway).ip.is_private:
            continue
        out.append(
            {
                "vlan": vid,
                "name": ifname,
                "interface": ifname,
                "device": device or None,
                "parent": parent_of(network, device),
                "gateway": gateway,
                "dhcp": dhcp_of(dhcp, ifname, gateway),
                "access": access_of(firewall, ifname, wans),
                "mdns": None,
            }
        )
    return sorted(out, key=lambda n: n["vlan"])


def norm_mac(value: Any) -> str:
    raw = re.sub(r"[^0-9a-f]", "", str(value or "").lower())
    return ":".join(raw[i : i + 2] for i in range(0, 12, 2)) if len(raw) == 12 else ""


def reservations(dhcp: Any) -> list[dict[str, Any]]:
    """dnsmasq's fixed addresses: `config host` with a MAC and an IP."""
    out = []
    for h in sections(dhcp, "host"):
        ip = str(h.get("ip") or "")
        for raw in as_list(h.get("mac")):
            mac = norm_mac(raw)
            if mac and ip:
                out.append({"mac": mac, "ip": ip, "name": str(h.get("name") or "") or None})
    return out


# --- changes --------------------------------------------------------------------


def add(config: str, kind: str, values: dict[str, Any], name: str | None = None) -> Op:
    args: dict[str, Any] = {"config": config, "type": kind, "values": values}
    if name:
        args["name"] = name
    return ("add", args)


def setv(config: str, section: str, values: dict[str, Any]) -> Op:
    return ("set", {"config": config, "section": section, "values": values})


def delete(config: str, section: str, option: str | None = None) -> Op:
    args = {"config": config, "section": section}
    if option:
        args["option"] = option
    return ("delete", args)


def new_ifname(network: Any, firewall: Any, vid: int, name: str) -> str:
    """A name for a new interface (and its zone): the network's name when it
    fits uci and the firewall (letters, digits, _; 11 characters), else vlanN."""
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:11]
    taken = {str(s.get(".name")) for s in sections(network, "interface")}
    taken |= {str(z.get("name")) for z in sections(firewall, "zone")}
    if slug and not slug[0].isdigit() and slug not in taken:
        return slug
    return f"vlan{vid}"


def plan_device(network: Any, vid: int, parent: str, wans: set[str]) -> tuple[str, list[Op]]:
    """The device of a new VLAN network: br-lan.30 on a bridge that filters
    VLANs (tagged on parent when it is one of the bridge's ports), or an 8021q
    device on a port outside any bridge."""
    if not parent:
        lan = next((s for s in sections(network, "interface") if s.get(".name") == "lan"), None)
        parent = re.sub(r"\.\d+$", "", str((lan or {}).get("device") or ""))
    if not parent:
        raise PluginError(
            f"no port to put VLAN {vid} on: name the router's port toward the switches"
        )
    if parent in wans:
        raise PluginError(f"{parent} is the router's WAN: a network goes on a LAN port")
    known = bridges(network)
    bridge, port = (parent, None) if parent in known else (bridge_of(network, parent), parent)
    if bridge is None:
        device = f"{parent}.{vid}"
        if any(d.get("name") == device for d in sections(network, "device")):
            return device, []
        return device, [
            add(
                "network",
                "device",
                {"type": "8021q", "ifname": parent, "vid": str(vid), "name": device},
            )
        ]
    bvs = bridge_vlans(network, bridge)
    if not bvs:
        raise filtering_off(bridge)
    current = next((s for s in bvs if str(s.get("vlan")) == str(vid)), None)
    ops: list[Op] = []
    if current is None:
        values: dict[str, Any] = {"device": bridge, "vlan": str(vid)}
        if port:
            values["ports"] = [f"{port}:t"]
        ops.append(add("network", "bridge-vlan", values))
    elif port and port not in {re.sub(r":.*$", "", p) for p in as_list(current.get("ports"))}:
        ops.append(
            setv(
                "network",
                current[".name"],
                {"ports": [*as_list(current.get("ports")), f"{port}:t"]},
            )
        )
    return f"{bridge}.{vid}", ops


def plan_address(iface: dict[str, Any], gateway: str) -> dict[str, Any]:
    """The interface's options for the gateway, in the form it already uses."""
    gw = ipaddress.IPv4Interface(gateway)
    current = as_list(iface.get("ipaddr"))
    if current and "/" in current[0]:
        return {"proto": "static", "ipaddr": str(gw)}
    return {"proto": "static", "ipaddr": str(gw.ip), "netmask": str(gw.netmask)}


def plan_dhcp(dhcp: Any, ifname: str, gateway: str, want: dict[str, Any] | None) -> list[Op]:
    """The `config dhcp` section of the interface: the range as start/limit
    (offsets in the subnet), the gateway handed out as router and DNS."""
    s = dhcp_section(dhcp, ifname)
    on = bool(want and want.get("enabled"))
    if not on:
        if s is None or str(s.get("ignore", "0")).lower() in ON:
            return []
        return [setv("dhcp", s[".name"], {"ignore": "1"})]
    gw = ipaddress.IPv4Interface(gateway)
    net = gw.network
    try:
        first = ipaddress.IPv4Address(str(want.get("from") if want else ""))
        last = ipaddress.IPv4Address(str(want.get("to") if want else ""))
    except ValueError as e:
        raise PluginError(f"the DHCP range is not two addresses: {e}") from e
    if first not in net or last not in net or last < first:
        raise PluginError(f"the DHCP range {first} - {last} is not inside {net}")
    if first <= gw.ip <= last:
        raise PluginError(f"the DHCP range {first} - {last} holds the gateway {gw.ip}")
    values: dict[str, Any] = {
        "interface": ifname,
        "start": str(int(first) - int(net.network_address)),
        "limit": str(int(last) - int(first) + 1),
    }
    options = [o for o in as_list((s or {}).get("dhcp_option")) if not o.startswith(("3,", "6,"))]
    values["dhcp_option"] = [*options, f"3,{gw.ip}", f"6,{gw.ip}"]
    if s is None:
        return [add("dhcp", "dhcp", {**values, "leasetime": "12h"}, ifname)]
    changed = {
        k: v
        for k, v in values.items()
        if (as_list(s.get(k)) if isinstance(v, list) else s.get(k)) != v
    }
    ops = [setv("dhcp", s[".name"], changed)] if changed else []
    if "ignore" in s:
        ops.append(delete("dhcp", s[".name"], "ignore"))
    return ops


def preset(access: str, zone: str, wan: str, label: str) -> tuple[dict[str, str], list[Op]]:
    """The zone's policies and the rules and forwardings of an access preset."""

    def rule(what: str, **values: Any) -> Op:
        return add("firewall", "rule", {"name": f"{PREFIX}{label} {what}", "src": zone, **values})

    dns = rule("DNS", proto="tcp udp", dest_port="53", target="ACCEPT")
    dhcp = rule("DHCP", proto="udp", dest_port="67-68", family="ipv4", target="ACCEPT")
    if access == "all":
        policy = {"input": "ACCEPT", "output": "ACCEPT", "forward": "ACCEPT"}
        return policy, [rule("to everything", dest="*", proto="all", target="ACCEPT")]
    policy = {"input": "REJECT", "output": "ACCEPT", "forward": "REJECT"}
    if access == "local":
        return policy, [dns, dhcp]
    return policy, [
        dns,
        dhcp,
        rule(
            "not to other networks", dest=wan, dest_ip=list(PRIVATE), proto="all", target="REJECT"
        ),
        add("firewall", "forwarding", {"src": zone, "dest": wan}),
    ]


def plan_access(
    firewall: Any, ifname: str, access: str | None, wans: set[str], label: str, vid: int
) -> list[Op]:
    """Replaces the rules of the network's zone with the access asked; custom
    (or none) leaves them. A zone shared with other networks is refused: its
    rules are theirs too."""
    if access not in ("internet", "all", "local") or access_of(firewall, ifname, wans) == access:
        return []
    wan = wan_zone(firewall, wans)
    z = zone_of(firewall, ifname)
    ops: list[Op] = []
    if z is not None and as_list(z.get("network")) != [ifname]:
        raise PluginError(
            f"the network {ifname} shares the firewall zone {z.get('name')} with "
            f"{', '.join(n for n in as_list(z.get('network')) if n != ifname)}: Omini changes "
            "only a zone of its own; change its rules in LuCI or keep the access Custom"
        )
    if z is None:
        name = ifname if len(ifname) <= 11 else f"vlan{vid}"
        policy, writes = preset(access, name, wan, label)
        ops.append(add("firewall", "zone", {"name": name, "network": [ifname], **policy}))
        return ops + writes
    name = str(z.get("name"))
    for r in sections(firewall, "rule"):
        if r.get("src") == name:
            ops.append(delete("firewall", r[".name"]))
    for f in sections(firewall, "forwarding"):
        if f.get("src") == name:
            ops.append(delete("firewall", f[".name"]))
    policy, writes = preset(access, name, wan, label)
    return [*ops, setv("firewall", z[".name"], policy), *writes]


def plan_set(
    configs: dict[str, Any], want: dict[str, Any], wans: set[str], host: str | None
) -> list[Op]:
    """network.set: the VLAN's device and interface, its address, DHCP and
    access, in place when the network exists (the LAN, VLAN 1, never moves)."""
    network, dhcp, firewall = configs["network"], configs["dhcp"], configs["firewall"]
    vid = int(want["vlan"])
    try:
        gateway = str(ipaddress.IPv4Interface(str(want.get("gateway") or "")))
    except ValueError as e:
        raise PluginError(f"the gateway is not an address with its prefix: {e}") from e
    if not ipaddress.IPv4Interface(gateway).ip.is_private:
        raise PluginError(f"the gateway {gateway} is not a private address")
    if want.get("mdns"):
        raise PluginError(
            "discovery between VLANs is not managed on OpenWrt: install umdns or avahi's "
            "reflector and set it up by hand (System → Software)"
        )
    access = want.get("access")
    if host and access in ("internet", "local"):
        try:
            mine = ipaddress.ip_address(host) in ipaddress.IPv4Interface(gateway).network
        except ValueError:
            mine = False
        if mine:
            raise PluginError(
                f"Omini reaches the router through this network ({host}): access "
                f"'{access}' would block the router's own web interface from it"
            )
    label = str(want.get("name") or "") or f"VLAN {vid}"
    ifname = interface_for(network, vid)
    ops: list[Op] = []
    if ifname is None:
        if vid == 1:
            raise PluginError("the router has no LAN to change")
        device, ops = plan_device(network, vid, str(want.get("parent") or ""), wans)
        ifname = new_ifname(network, firewall, vid, str(want.get("name") or ""))
        ops.append(
            add("network", "interface", {"device": device, **plan_address({}, gateway)}, ifname)
        )
    else:
        iface = by_name(network, ifname) or {}
        if gateway_of(iface) != gateway:
            ops.append(setv("network", ifname, plan_address(iface, gateway)))
    ops += plan_dhcp(dhcp, ifname, gateway, want.get("dhcp"))
    ops += plan_access(firewall, ifname, access, wans, label, vid)
    return ops


def plan_delete(configs: dict[str, Any], vid: int, wans: set[str]) -> list[Op]:
    """network.delete: the network's firewall rules and zone, its DHCP, the
    interface, and the VLAN when no port is in it any more."""
    if vid == 1:
        raise PluginError("the LAN cannot be removed from Omini")
    network, dhcp, firewall = configs["network"], configs["dhcp"], configs["firewall"]
    n = next((n for n in networks(network, dhcp, firewall, wans) if n["vlan"] == vid), None)
    if n is None:
        raise PluginError(f"the router serves no network on VLAN {vid}")
    ifname, device = n["interface"], str(n["device"] or "")
    ops: list[Op] = []
    z = zone_of(firewall, ifname)
    if z is not None:
        rest = [x for x in as_list(z.get("network")) if x != ifname]
        if rest:
            ops.append(setv("firewall", z[".name"], {"network": rest}))
        else:
            name = str(z.get("name"))
            for kind in ("rule", "forwarding", "redirect"):
                for r in sections(firewall, kind):
                    if name in (r.get("src"), r.get("dest")):
                        ops.append(delete("firewall", r[".name"]))
            ops.append(delete("firewall", z[".name"]))
    for s in sections(dhcp, "dhcp"):
        if s.get("interface") == ifname:
            ops.append(delete("dhcp", s[".name"]))
    ops.append(delete("network", ifname))
    for d in sections(network, "device"):
        if d.get("name") == device and d.get("type") in ("8021q", "8021ad"):
            ops.append(delete("network", d[".name"]))
    for bv in bridge_vlans(network):
        if f"{bv.get('device')}.{bv.get('vlan')}" == device and not as_list(bv.get("ports")):
            ops.append(delete("network", bv[".name"]))
    return ops


def hostname(name: str) -> str:
    """A name dnsmasq can hand out: letters, digits and dashes."""
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:63]


def plan_reserve(dhcp: Any, want: dict[str, Any]) -> list[Op]:
    mac = norm_mac(want.get("mac"))
    try:
        ip = str(ipaddress.IPv4Address(str(want.get("ip") or "")))
    except ValueError as e:
        raise PluginError(f"a reservation needs an IPv4 address: {e}") from e
    if not mac:
        raise PluginError("a reservation needs a MAC address")
    values: dict[str, Any] = {"mac": mac, "ip": ip}
    name = hostname(str(want.get("name") or ""))
    if name:
        values["name"] = name
    hosts = [
        h for h in sections(dhcp, "host") if mac in {norm_mac(m) for m in as_list(h.get("mac"))}
    ]
    if not hosts:
        return [add("dhcp", "host", values)]
    h = hosts[0]
    if (norm_mac(as_list(h.get("mac"))[0]), str(h.get("ip") or ""), str(h.get("name") or "")) == (
        mac,
        ip,
        values.get("name", ""),
    ):
        return []
    ops = [setv("dhcp", h[".name"], values)]
    if not name and h.get("name"):
        ops.append(delete("dhcp", h[".name"], "name"))
    return ops


def plan_unreserve(dhcp: Any, mac_value: Any) -> list[Op]:
    mac = norm_mac(mac_value)
    return [
        delete("dhcp", h[".name"])
        for h in sections(dhcp, "host")
        if mac in {norm_mac(m) for m in as_list(h.get("mac"))}
    ]


def matches(configs: dict[str, Any], want: dict[str, Any], wans: set[str]) -> bool:
    """Whether the router's configuration serves want as asked."""
    have = next(
        (
            n
            for n in networks(configs["network"], configs["dhcp"], configs["firewall"], wans)
            if n["vlan"] == int(want["vlan"])
        ),
        None,
    )
    if have is None or have["gateway"] != str(ipaddress.IPv4Interface(str(want["gateway"]))):
        return False
    d, h = want.get("dhcp") or {"enabled": False}, have["dhcp"]
    if bool(d.get("enabled")) != bool(h.get("enabled")):
        return False
    if d.get("enabled") and (d.get("from"), d.get("to")) != (h.get("from"), h.get("to")):
        return False
    access = want.get("access")
    return access not in ("internet", "all", "local") or have["access"] == access
