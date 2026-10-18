import copy

import pytest
from omini_sdk import Change, PluginError

import omini_openwrt.collect as collect_module
from omini_openwrt import wifi
from omini_openwrt.collect import apply


def config(*sections):
    return {"values": {s[".name"]: s for s in sections}}


NETWORK = config(
    {".name": "lan", ".type": "interface", "device": "br-lan", "proto": "static"},
    {".name": "iot", ".type": "interface", "device": "br-lan.30", "proto": "none"},
    {".name": "cams", ".type": "interface", "device": "cams0", "proto": "none"},
    {".name": "cfg1", ".type": "device", "name": "cams0", "type": "8021q", "vid": "40"},
    {
        ".name": "cfg2",
        ".type": "bridge-vlan",
        "device": "br-lan",
        "vlan": "1",
        "ports": ["lan1:u*", "lan2:u*"],
    },
)
WIRELESS = config(
    {".name": "radio0", ".type": "wifi-device"},
    {".name": "radio1", ".type": "wifi-device"},
    {
        ".name": "a",
        ".type": "wifi-iface",
        "device": "radio0",
        "mode": "ap",
        "ssid": "Home",
        "encryption": "sae-mixed",
        "network": "lan",
    },
    {
        ".name": "b",
        ".type": "wifi-iface",
        "device": "radio1",
        "mode": "ap",
        "ssid": "Home",
        "encryption": "sae-mixed",
        "network": "lan",
        "disabled": "1",
    },
    {
        ".name": "c",
        ".type": "wifi-iface",
        "device": "radio0",
        "mode": "ap",
        "ssid": "Things",
        "encryption": "psk2",
        "network": ["iot"],
    },
    {
        ".name": "d",
        ".type": "wifi-iface",
        "device": "radio1",
        "mode": "ap",
        "ssid": "Cams",
        "encryption": "none",
        "network": "cams",
        "disabled": "1",
    },
    {
        ".name": "e",
        ".type": "wifi-iface",
        "device": "radio1",
        "mode": "sta",
        "ssid": "Upstream",
        "network": "wwan",
    },
)


def test_each_ssid_once_with_its_vlan():
    got = {w["ssid"]: w for w in wifi.wifi_networks(WIRELESS, NETWORK)}
    assert set(got) == {"Home", "Things", "Cams"}  # a client (sta) interface is no network of ours
    assert got["Home"] == {"ssid": "Home", "vlan": None, "security": "wpa2wpa3", "enabled": True}
    assert (got["Things"]["vlan"], got["Things"]["security"]) == (30, "wpa2")
    assert (got["Cams"]["vlan"], got["Cams"]["security"], got["Cams"]["enabled"]) == (
        40,
        "open",
        False,
    )


def test_the_interface_of_a_vlan():
    assert wifi.plan_interface(NETWORK, 30) == ("iot", [])
    assert wifi.plan_interface(NETWORK, 1) == ("lan", [])
    name, writes = wifi.plan_interface(NETWORK, 50)
    assert name == "vlan50"
    assert writes == [
        ("bridge-vlan", {"device": "br-lan", "vlan": "50", "ports": ["lan1:t", "lan2:t"]}),
        ("interface", {"name": "vlan50", "proto": "none", "device": "br-lan.50"}),
    ]


def test_a_bridge_that_does_not_filter_vlans_is_left_alone():
    plain = config({".name": "lan", ".type": "interface", "device": "br-lan"})
    with pytest.raises(PluginError, match="Bridge VLAN filtering"):
        wifi.plan_interface(plain, 50)


# --- wifi.set and wifi.delete on the fake router ----------------------------


@pytest.fixture
def router(openwrt, monkeypatch):
    monkeypatch.setattr(collect_module, "VERIFY_S", 0)
    monkeypatch.setattr(collect_module, "VERIFY_PAUSE_S", 0)
    openwrt.other["wireless"] = copy.deepcopy(WIRELESS)
    return openwrt


def wifi_change(kind="wifi.set", **value):
    return Change(device="94:83:c4:00:00:01", kind=kind, value=value)


def aps(openwrt):
    return {k: v for k, v in openwrt.other["wireless"]["values"].items() if k in "abcde"}


def test_a_password_secures_only_the_open_access_points(router, cfg):
    w = router.other["wireless"]["values"]
    w["a"]["encryption"] = "none"  # Home on radio0 was open; radio1's is WPA3 (sae)
    w["b"]["encryption"] = "sae"
    apply(cfg, wifi_change(ssid="Home", password="a-long-password"))
    got = aps(router)
    assert got["a"]["encryption"] == "psk2"
    assert got["b"]["encryption"] == "sae"  # not downgraded by the open one before it
    assert got["a"]["key"] == got["b"]["key"] == "a-long-password"


def test_security_is_named_only_when_the_model_has_it():
    assert wifi.security_of("psk2+ccmp") == "wpa2"
    assert wifi.security_of("wpa2") == "wpa2"  # WPA2-Enterprise
    assert wifi.security_of("sae") == wifi.security_of("wpa3") == "wpa3"
    assert wifi.security_of("sae-mixed") == wifi.security_of("wpa3-mixed") == "wpa2wpa3"
    assert wifi.security_of(None) == wifi.security_of("owe") == "open"
    # WPA (1) alone or mixed with WPA2, and WEP, are none of open, wpa2, wpa3.
    for weak in ("psk", "psk-mixed", "psk-mixed+tkip", "wpa", "wpa-mixed", "wep-open"):
        assert wifi.security_of(weak) is None


def test_a_new_network_uses_wpa3_on_6_ghz_radios(router, cfg):
    w = router.other["wireless"]["values"]
    w["radio0"]["band"] = "5g"
    w["radio1"]["band"] = "6g"
    apply(cfg, wifi_change(ssid="Guest", password="a-long-password"))
    new = [s for s in router.other["wireless"]["values"].values() if s.get("ssid") == "Guest"]
    assert {s["device"]: s["encryption"] for s in new} == {"radio0": "psk2", "radio1": "sae"}
    # An open access point given a password on a 6 GHz radio gets SAE too.
    apply(cfg, wifi_change(ssid="Cams", password="a-long-password"))
    assert router.other["wireless"]["values"]["d"]["encryption"] == "sae"


def test_no_vlan_moves_the_network_back_to_the_lan(router, cfg):
    apply(cfg, wifi_change(ssid="Things", vlan=None))  # it was on iot (VLAN 30)
    assert aps(router)["c"]["network"] == "lan"
