"""Wi-Fi radios of an OpenWrt access point and the channel each one is on,
read every round and changed by wifi.channel.

- The configuration: /etc/config/wireless's `config wifi-device` sections
  (radio0, radio1...): `band` (2g, 5g, 6g; `hwmode` 11g / 11a before 21.02),
  `channel` (a number, or `auto` / `0`: the driver picks it), `htmode` (HT20,
  VHT80, HE160, EHT320...: the width), `disabled`.
- What the radio does now: iwinfo's `info` of the radio's interfaces
  (`channel`, `txpower`, `frequency`, `htmode`), found through netifd's
  `network.wireless status` (each radio's `interfaces[].ifname`), else by
  asking iwinfo for the radio itself (`iwinfo info {"device": "radio0"}`).
- A wireless uplink: a `config wifi-iface` in mode `sta` (a client of another
  access point, a repeater or a Wi-Fi WAN) or `mesh` (802.11s), not disabled.

Everything here reads or plans uci calls; collect.apply stages, applies (with
rollback), verifies and confirms them."""

from __future__ import annotations

import re
from typing import Any

from omini_sdk import PluginError

from omini_openwrt.wifi import sections

ON = {"1", "true", "on", "yes", "enabled"}
BANDS = {"2g": "2.4ghz", "5g": "5ghz", "6g": "6ghz"}
HWMODES = {"11b": "2.4ghz", "11g": "2.4ghz", "11a": "5ghz"}
WIDTHS = {20, 40, 80, 160, 320}
# The channels of each band (802.11): 2.4 GHz 1-14, 5 GHz 32-177, 6 GHz 1-233.
CHANNELS = {"2.4ghz": range(1, 15), "5ghz": range(32, 178), "6ghz": range(1, 234)}
UPLINK_MODES = {"sta", "mesh"}


def off(value: Any) -> bool:
    return str(value or "0").strip().lower() in ON


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def band_of_frequency(freq: Any) -> str | None:
    f = as_int(freq)
    if not f:
        return None
    if 2400 <= f < 2500:
        return "2.4ghz"
    if 4900 <= f < 5925:
        return "5ghz"
    if 5925 <= f <= 7125:
        return "6ghz"
    return None


def width_of(htmode: Any) -> int | None:
    """htmode (HT20, VHT80, HE160, EHT320, NOHT) → MHz."""
    mode = str(htmode or "").strip().upper()
    if mode == "NOHT":
        return 20
    m = re.fullmatch(r"(?:HT|VHT|HE|EHT)(\d+)", mode)
    width = int(m.group(1)) if m else None
    return width if width in WIDTHS else None


def configured_channel(radio: dict[str, Any]) -> tuple[int | None, bool | None]:
    """The radio's `channel`: (the fixed channel, whether it is automatic)."""
    raw = radio.get("channel")
    if raw is None:
        return None, None
    value = str(raw).strip().lower()
    if value in ("auto", "0"):
        return None, True
    n = as_int(value)
    return (n, False) if n and 1 <= n <= 233 else (None, None)


def interfaces_of(status: Any, radio: str) -> list[str] | None:
    """The interfaces (phy0-ap0...) netifd runs on a radio; None when netifd's
    wireless status is not known."""
    if not isinstance(status, dict):
        return None
    st = status.get(radio)
    if not isinstance(st, dict):
        return []
    out = []
    for i in st.get("interfaces") or []:
        name = i.get("ifname") if isinstance(i, dict) else None
        if isinstance(name, str) and name:
            out.append(name)
    return out


def radios(wireless: Any, status: Any, infos: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Each wifi-device as Radio fields. infos: iwinfo `info` by interface
    (and by radio name, when iwinfo was asked for the radio itself)."""
    out = []
    for r in sections(wireless, "wifi-device"):
        name = r.get(".name")
        if not isinstance(name, str):
            continue
        info = live_info(name, status, infos)
        band = (
            BANDS.get(str(r.get("band") or "").lower())
            or HWMODES.get(str(r.get("hwmode") or "").lower())
            or band_of_frequency(info.get("frequency"))
        )
        fixed, auto = configured_channel(r)
        channel = as_int(info.get("channel"))
        if channel is None or not 1 <= channel <= 233:
            channel = fixed
        if band is None and channel is not None and channel <= 14 and fixed is not None:
            band = "2.4ghz"
        if band is None:
            continue
        power = info.get("txpower")
        out.append(
            {
                "name": name,
                "band": band,
                "channel": channel,
                "width_mhz": width_of(r.get("htmode")) or width_of(info.get("htmode")),
                "auto": auto,
                "tx_power_dbm": float(power)
                if isinstance(power, (int, float)) and not isinstance(power, bool)
                else None,
                "enabled": not off(r.get("disabled")),
            }
        )
    return out


def live_info(radio: str, status: Any, infos: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """iwinfo's info of one of the radio's interfaces that has a channel."""
    names = interfaces_of(status, radio)
    for n in [*(names or []), radio]:
        info = infos.get(n)
        if isinstance(info, dict) and as_int(info.get("channel")):
            return info
    return {}


def wireless_uplink(wireless: Any, infos: dict[str, dict[str, Any]]) -> bool | None:
    """Whether the device reaches the network over Wi-Fi: a client (sta) or
    mesh interface in the configuration (on a radio that is on); without the
    configuration, an interface iwinfo shows in client or mesh mode."""
    if isinstance(wireless, dict) and wireless.get("values"):
        disabled = {
            r.get(".name") for r in sections(wireless, "wifi-device") if off(r.get("disabled"))
        }
        return any(
            str(w.get("mode") or "ap").lower() in UPLINK_MODES
            and not off(w.get("disabled"))
            and w.get("device") not in disabled
            for w in sections(wireless, "wifi-iface")
        )
    modes = {str(i.get("mode") or "").lower() for i in infos.values() if isinstance(i, dict)}
    if not modes:
        return None
    return bool(modes & {"client", "mesh point"})


def check_channel(wireless: Any, radio: str, channel: int) -> dict[str, Any]:
    """The radio's section, when the channel can be set on it."""
    section = next((r for r in sections(wireless, "wifi-device") if r.get(".name") == radio), None)
    if section is None:
        raise PluginError(f"OpenWrt has no Wi-Fi radio {radio}")
    if off(section.get("disabled")):
        raise PluginError(f"the Wi-Fi radio {radio} is off: turn it on in LuCI first")
    band = BANDS.get(str(section.get("band") or "").lower()) or HWMODES.get(
        str(section.get("hwmode") or "").lower()
    )
    if band and channel not in CHANNELS[band]:
        raise PluginError(f"channel {channel} is not a channel of the {radio} radio's band")
    return section


def plan_channel(radio: str, channel: int) -> list[tuple[str, dict[str, Any]]]:
    return [("set", {"config": "wireless", "section": radio, "values": {"channel": str(channel)}})]
