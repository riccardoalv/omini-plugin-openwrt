"""VLANs of an OpenWrt switch (DSA bridge VLAN filtering): the ports' membership
(port.vlans) and the VLANs themselves (vlan.set, vlan.delete).

A bridge filters VLANs when /etc/config/network has `config bridge-vlan`
sections for it, one per VLAN, whose `ports` list says each port's membership:
`lan1:u*` (untagged, the port's PVID), `lan2:t` (tagged), a bare `lan3`
(untagged). The bridge itself (the router's CPU) is in every VLAN through the
section's `local` option, which the plugin never touches. The plugin never
turns filtering on: a bridge without bridge-vlan sections is refused with the
way to turn it on in LuCI. The VLAN's name goes in the section's `description`
option (netifd ignores it).

Everything here plans uci calls; collect.apply stages, applies (with
rollback), verifies and confirms them."""

from __future__ import annotations

import re
from typing import Any

from omini_sdk import PluginError

from omini_openwrt.wifi import sections

# One uci call: (method, args).
Op = tuple[str, dict[str, Any]]


def vlan_id(value: Any, key: str, kind: str) -> int:
    """The VLAN id of a change's value, 1 to 4094."""
    raw = value.get(key) if isinstance(value, dict) else None
    try:
        vid = int(raw) if raw is not None and not isinstance(raw, bool) else None
    except (TypeError, ValueError):
        vid = None
    if vid is None:
        raise PluginError(f"{kind} needs a VLAN id ({key}), not {raw!r}")
    if not 1 <= vid <= 4094:
        raise PluginError("VLAN ids go from 1 to 4094")
    return vid


def wanted(value: Any) -> tuple[int | None, list[int]]:
    """port.vlans' {untagged, tagged}, checked: ids 1 to 4094, no VLAN both
    untagged and tagged, at least one VLAN."""
    if not isinstance(value, dict):
        raise PluginError(f"cannot apply port.vlans with value {value!r}")
    untagged, tagged = value.get("untagged"), value.get("tagged") or []
    try:
        u = int(untagged) if untagged is not None else None
        t = sorted({int(v) for v in tagged})
    except (TypeError, ValueError) as e:
        raise PluginError(f"cannot apply port.vlans with value {value!r}") from e
    if any(not 1 <= v <= 4094 for v in [*t, *([u] if u is not None else [])]):
        raise PluginError("VLAN ids go from 1 to 4094")
    if u is not None and u in t:
        raise PluginError(f"VLAN {u} cannot be both untagged and tagged on one port")
    if u is None and not t:
        raise PluginError("a port needs an untagged VLAN or at least one tagged VLAN")
    return u, t


def as_list(value: Any) -> list[str]:
    """A uci option that may be a list or a space-separated string."""
    if isinstance(value, list):
        return [str(v) for v in value]
    return str(value or "").split()


def port_name(entry: str) -> str:
    return re.sub(r":.*$", "", entry.strip())


def bridges(network: Any) -> dict[str, list[str]]:
    """The bridges of the configuration (`config device` of type bridge) and their ports."""
    return {
        str(s["name"]): as_list(s.get("ports"))
        for s in sections(network, "device")
        if s.get("type") == "bridge" and s.get("name")
    }


def bridge_of(network: Any, port: str) -> str | None:
    return next((b for b, ports in bridges(network).items() if port in ports), None)


def bridge_vlans(network: Any, bridge: str | None = None) -> list[dict[str, Any]]:
    return [
        s
        for s in sections(network, "bridge-vlan")
        if bridge is None or str(s.get("device") or "") == bridge
    ]


def vid_of(section: dict[str, Any]) -> int | None:
    v = str(section.get("vlan") or "")
    return int(v) if v.isdigit() else None


def names(network: Any) -> dict[int, str]:
    """The VLANs' names saved in their bridge-vlan sections."""
    out: dict[int, str] = {}
    for s in bridge_vlans(network):
        vid, name = vid_of(s), str(s.get("description") or "").strip()
        if vid and name:
            out.setdefault(vid, name)
    return out


def filtering_off(bridge: str) -> PluginError:
    return PluginError(
        f"OpenWrt's bridge {bridge} does not filter VLANs, and Omini does not turn that on by "
        f"itself: in LuCI, open Network → Interfaces → Devices → {bridge} → Configure… → "
        "Bridge VLAN filtering, tick Enable VLAN filtering, add VLAN 1 with every LAN port "
        f"untagged and primary (PVID), change the lan interface's device to {bridge}.1, "
        "then Save & Apply; and try again"
    )


def main_bridge(network: Any) -> str:
    """The bridge VLANs go on: the LAN's (its interface rides on it), else the
    first one that filters VLANs."""
    lan = next((s for s in sections(network, "interface") if s.get(".name") == "lan"), None)
    known = bridges(network)
    if lan is not None:
        base = re.sub(r"\.\d+$", "", str(lan.get("device") or ""))
        if base in known:
            return base
    filtering = [b for b in known if bridge_vlans(network, b)]
    if filtering:
        return filtering[0]
    if known:
        return next(iter(known))
    raise PluginError("OpenWrt has no bridge to put VLANs on")


def plan_port(network: Any, port: str, untagged: int | None, tagged: list[int]) -> list[Op]:
    """port.vlans: the port's entry in every bridge-vlan section of its bridge,
    replaced by exactly the membership asked. Other ports are not touched."""
    bridge = bridge_of(network, port)
    if bridge is None:
        raise PluginError(f"{port} is not a port of a bridge: its VLANs cannot change")
    bvs = bridge_vlans(network, bridge)
    if not bvs:
        raise filtering_off(bridge)
    have = {vid_of(s) for s in bvs}
    for vid in ([untagged] if untagged else []) + tagged:
        if vid not in have:
            raise PluginError(f"OpenWrt has no VLAN {vid} on {bridge}: create it first")
    ops: list[Op] = []
    for s in bvs:
        vid = vid_of(s)
        ports = as_list(s.get("ports"))
        kept = [p for p in ports if port_name(p) != port]
        if vid == untagged:
            kept.append(f"{port}:u*")
        elif vid in tagged:
            kept.append(f"{port}:t")
        if kept == ports:
            continue
        if kept:
            ops.append(
                ("set", {"config": "network", "section": s[".name"], "values": {"ports": kept}})
            )
        else:
            ops.append(("delete", {"config": "network", "section": s[".name"], "option": "ports"}))
    return ops


def plan_set(network: Any, vid: int, name: str) -> tuple[str, list[Op]]:
    """vlan.set: a bridge-vlan section with no port (the router's CPU only), or
    the existing one renamed. Returns the bridge and the uci calls."""
    bridge = main_bridge(network)
    bvs = bridge_vlans(network, bridge)
    if not bvs:
        raise filtering_off(bridge)
    current = next((s for s in bvs if vid_of(s) == vid), None)
    if current is not None:
        if str(current.get("description") or "") == name:
            return bridge, []
        if not name:
            return bridge, [
                (
                    "delete",
                    {"config": "network", "section": current[".name"], "option": "description"},
                )
            ]
        return bridge, [
            (
                "set",
                {
                    "config": "network",
                    "section": current[".name"],
                    "values": {"description": name},
                },
            )
        ]
    values: dict[str, Any] = {"device": bridge, "vlan": str(vid)}
    if name:
        values["description"] = name
    return bridge, [("add", {"config": "network", "type": "bridge-vlan", "values": values})]


def plan_delete(network: Any, vid: int) -> list[Op]:
    """vlan.delete: the VLAN's bridge-vlan sections; refused while a router
    interface rides on it (its network goes first)."""
    if vid == 1:
        raise PluginError("VLAN 1 cannot be deleted")
    found = [s for s in bridge_vlans(network) if vid_of(s) == vid]
    for s in found:
        device = f"{s.get('device')}.{vid}"
        users = [
            str(i[".name"]) for i in sections(network, "interface") if i.get("device") == device
        ]
        if users:
            raise PluginError(
                f"the router's interface {', '.join(users)} uses VLAN {vid}: "
                "delete its network first"
            )
    return [("delete", {"config": "network", "section": s[".name"]}) for s in found]
