"""Wi-Fi networks of an OpenWrt access point (/etc/config/wireless) and the
VLAN each one puts its clients in, read every round and changed by wifi.set
and wifi.delete.

An access point (`config wifi-iface`, mode ap) is attached to a network
interface (`option network`), whose device says the VLAN: `br-lan.30` (a
bridge VLAN) or a `config device` of type 8021q; the LAN's bridge alone is
VLAN 1. A VLAN with no interface on the router yet gets one when the bridge
already filters VLANs (`config bridge-vlan` sections): a bridge VLAN tagged
on the ports VLAN 1 has, and an unmanaged interface on it. The password is
written to the router only."""

from __future__ import annotations

import re
from typing import Any

from omini_sdk import PluginError

SECURITY = {"none": "open", "owe": "open", "psk2": "wpa2", "psk": "wpa2", "psk-mixed": "wpa2"}


def sections(config: Any, kind: str) -> list[dict[str, Any]]:
    values = config.get("values") if isinstance(config, dict) else None
    if not isinstance(values, dict):
        return []
    return [s for s in values.values() if isinstance(s, dict) and s.get(".type") == kind]


def security_of(encryption: Any) -> str:
    e = str(encryption or "none").split("+")[0]
    if e.startswith("sae-mixed"):
        return "wpa2wpa3"
    if e.startswith("sae"):
        return "wpa3"
    return SECURITY.get(e, "wpa2")


def networks_of(iface: dict[str, Any]) -> list[str]:
    n = iface.get("network")
    return [str(x) for x in n] if isinstance(n, list) else str(n or "").split()


def vlan_of_interface(network: Any, name: str) -> int | None:
    """The VLAN of a network interface, by its device."""
    iface = next((s for s in sections(network, "interface") if s.get(".name") == name), None)
    if iface is None:
        return None
    device = str(iface.get("device") or "")
    m = re.search(r"\.(\d{1,4})$", device)
    if m:
        return int(m.group(1))
    for d in sections(network, "device"):
        if (
            d.get("name") == device
            and d.get("type") in ("8021q", "8021ad")
            and str(d.get("vid", "")).isdigit()
        ):
            return int(d["vid"])
    return 1 if name == "lan" else None


def wifi_networks(wireless: Any, network: Any) -> list[dict[str, Any]]:
    """Each SSID the router broadcasts (once, whatever the radios)."""
    out: dict[str, dict[str, Any]] = {}
    for w in sections(wireless, "wifi-iface"):
        ssid = str(w.get("ssid") or "")
        if w.get("mode", "ap") != "ap" or not ssid:
            continue
        nets = networks_of(w)
        vid = vlan_of_interface(network, nets[0]) if nets else None
        on = str(w.get("disabled", "0")) not in ("1", "true", "on", "yes")
        if ssid in out:
            out[ssid]["enabled"] = out[ssid]["enabled"] or on
            continue
        out[ssid] = {
            "ssid": ssid,
            "vlan": vid if vid and vid != 1 else None,
            "security": security_of(w.get("encryption")),
            "enabled": on,
        }
    return list(out.values())


def interface_for(network: Any, vid: int) -> str | None:
    for s in sections(network, "interface"):
        if vlan_of_interface(network, str(s.get(".name"))) == vid:
            return str(s[".name"])
    return None


def plan_interface(network: Any, vid: int) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    """The interface of a VLAN, and the uci writes that create it (a bridge VLAN
    and an unmanaged interface) when the router has none."""
    found = interface_for(network, vid)
    if found:
        return found, []
    bridge_vlans = sections(network, "bridge-vlan")
    lan = next((b for b in bridge_vlans if str(b.get("vlan")) == "1"), None)
    if lan is None:
        raise PluginError(
            f"OpenWrt has no interface on VLAN {vid}, and its bridge does not filter VLANs: "
            "add the VLAN to the bridge in LuCI (Network → Interfaces → Devices → br-lan → "
            "Bridge VLAN filtering) first"
        )
    bridge = str(lan.get("device") or "br-lan")
    ports = [re.sub(r":.*$", "", str(p)) + ":t" for p in lan.get("ports") or []]
    name = f"vlan{vid}"
    return name, [
        ("bridge-vlan", {"device": bridge, "vlan": str(vid), "ports": ports}),
        ("interface", {"name": name, "proto": "none", "device": f"{bridge}.{vid}"}),
    ]
