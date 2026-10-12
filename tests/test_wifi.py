import pytest
from omini_sdk import PluginError

from omini_openwrt import wifi


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
