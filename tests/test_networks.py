"""network.set, network.delete and the reservations against the fake router's
network, dhcp and firewall configs."""

import copy

import pytest
from omini_sdk import Change, PluginError

import omini_openwrt.collect as collect_module
from omini_openwrt import networks as nets
from omini_openwrt.collect import apply, collect
from tests.conftest import DHCP, FIREWALL, sec
from tests.test_vlans import FILTERING


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(collect_module, "VERIFY_S", 0)
    monkeypatch.setattr(collect_module, "VERIFY_PAUSE_S", 0)


@pytest.fixture
def router(openwrt):
    openwrt.network = copy.deepcopy(FILTERING)
    openwrt.reload()
    return openwrt


def change(kind, value):
    return Change(device="94:83:c4:00:00:01", kind=kind, value=value)


CAMERAS = {
    "vlan": 30,
    "name": "Cameras",
    "gateway": "192.168.30.1/24",
    "dhcp": {"enabled": True, "from": "192.168.30.100", "to": "192.168.30.200"},
    "access": "internet",
}


def networks(devices):
    return {n.vlan: n for n in devices[0].networks or []}


def omini_rules(openwrt):
    return sorted(
        s["name"]
        for s in openwrt.other["firewall"]["values"].values()
        if s[".type"] == "rule" and s.get("name", "").startswith("Omini: ")
    )


def test_reads_the_networks(router, cfg):
    router.other["dhcp"]["values"]["cfg07411c"] = sec(
        "cfg07411c", "host", name="printer", mac="AA:BB:CC:00:00:01", ip="192.168.1.50"
    )
    [dev] = collect(cfg)
    by = networks([dev])
    assert set(by) == {1, 20}  # the WAN is not a network of the LAN
    lan = by[1]
    assert (lan.interface, lan.device, lan.parent, lan.gateway) == (
        "lan",
        "br-lan.1",
        "br-lan",
        "192.168.1.1/24",
    )
    assert lan.dhcp.enabled and (lan.dhcp.from_, lan.dhcp.to) == ("192.168.1.100", "192.168.1.249")
    assert lan.access == "custom"  # OpenWrt's LAN zone is none of the presets
    assert lan.mdns is None
    iot = by[20]
    assert (iot.gateway, iot.dhcp.enabled, iot.access) == ("10.0.20.1/24", False, "custom")
    assert [(r.mac, r.ip, r.name) for r in dev.dhcp_reservations] == [
        ("aa:bb:cc:00:00:01", "192.168.1.50", "printer")
    ]


def test_creates_a_network_and_deletes_it(router, cfg):
    devices = apply(cfg, change("network.set", CAMERAS))
    cams = networks(devices)[30]
    assert (cams.interface, cams.device, cams.gateway, cams.access) == (
        "cameras",
        "br-lan.30",
        "192.168.30.1/24",
        "internet",
    )
    assert (cams.dhcp.from_, cams.dhcp.to) == ("192.168.30.100", "192.168.30.200")
    assert router.confirmed == 1 and router.pending is None
    values = router.network["values"]
    assert values["cameras"] == {
        ".anonymous": False,
        ".type": "interface",
        ".name": "cameras",
        "device": "br-lan.30",
        "proto": "static",
        "ipaddr": "192.168.30.1",
        "netmask": "255.255.255.0",
    }
    # The VLAN on the bridge, with no port: the router's CPU only.
    bv = next(s for s in values.values() if s[".type"] == "bridge-vlan" and s["vlan"] == "30")
    assert "ports" not in bv
    dhcp = router.other["dhcp"]["values"]["cameras"]
    assert (dhcp["start"], dhcp["limit"], dhcp["dhcp_option"]) == (
        "100",
        "101",
        ["3,192.168.30.1", "6,192.168.30.1"],
    )
    fw = router.other["firewall"]["values"]
    zone = next(s for s in fw.values() if s[".type"] == "zone" and s["name"] == "cameras")
    assert (zone["network"], zone["input"], zone["forward"]) == (["cameras"], "REJECT", "REJECT")
    assert {"src": "cameras", "dest": "wan"} in [
        {k: s[k] for k in ("src", "dest")} for s in fw.values() if s[".type"] == "forwarding"
    ]
    assert omini_rules(router) == [
        "Omini: Cameras DHCP",
        "Omini: Cameras DNS",
        "Omini: Cameras not to other networks",
    ]
    block = next(s for s in fw.values() if s.get("name") == "Omini: Cameras not to other networks")
    assert block["dest_ip"] == ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    assert block["target"] == "REJECT" and block["dest"] == "wan"

    # Asked again: nothing to do.
    n = len(router.writes)
    apply(cfg, change("network.set", CAMERAS))
    assert len(router.writes) == n

    devices = apply(cfg, change("network.delete", {"vlan": 30}))
    assert 30 not in networks(devices)
    assert router.network == FILTERING
    assert router.other == {"dhcp": DHCP, "firewall": FIREWALL}


def test_changes_the_access_in_place(router, cfg):
    apply(cfg, change("network.set", CAMERAS))
    devices = apply(cfg, change("network.set", {**CAMERAS, "access": "local"}))
    assert networks(devices)[30].access == "local"
    assert omini_rules(router) == ["Omini: Cameras DHCP", "Omini: Cameras DNS"]
    fw = router.other["firewall"]["values"].values()
    assert not [s for s in fw if s[".type"] == "forwarding" and s["src"] == "cameras"]
    devices = apply(cfg, change("network.set", {**CAMERAS, "access": "all"}))
    assert networks(devices)[30].access == "all"
    assert omini_rules(router) == ["Omini: Cameras to everything"]
    # Custom leaves the rules as they are, whatever they are.
    n = len(router.writes)
    devices = apply(cfg, change("network.set", {**CAMERAS, "access": "custom"}))
    assert len(router.writes) == n and networks(devices)[30].access == "all"


def test_a_vlan_on_a_port_toward_the_switches(router, cfg):
    apply(cfg, change("network.set", {**CAMERAS, "parent": "lan3"}))
    bv = next(
        s
        for s in router.network["values"].values()
        if s[".type"] == "bridge-vlan" and s["vlan"] == "30"
    )
    assert bv["ports"] == ["lan3:t"]  # only the port named
    # A port outside every bridge gets an 802.1Q device.
    devices = apply(
        cfg,
        change(
            "network.set",
            {
                **CAMERAS,
                "vlan": 40,
                "gateway": "10.40.0.1/24",
                "dhcp": {"enabled": False},
                "name": "Lab",
                "parent": "eth2",
            },
        ),
    )
    lab = networks(devices)[40]
    assert (lab.device, lab.parent, lab.interface) == ("eth2.40", "eth2", "lab")
    dev = next(s for s in router.network["values"].values() if s.get("name") == "eth2.40")
    assert (dev["type"], dev["ifname"], dev["vid"]) == ("8021q", "eth2", "40")
    with pytest.raises(PluginError, match="eth1 is the router's WAN"):
        apply(cfg, change("network.set", {**CAMERAS, "vlan": 50, "parent": "eth1"}))


def test_the_lan_changes_in_place(router, cfg):
    lan = {
        "vlan": 1,
        "gateway": "192.168.1.1/24",
        "dhcp": {"enabled": True, "from": "192.168.1.20", "to": "192.168.1.99"},
        "access": "custom",
    }
    devices = apply(cfg, change("network.set", lan))
    assert [(m, a["config"]) for m, a in router.writes if m != "apply"] == [("set", "dhcp")]
    got = networks(devices)[1]
    assert (got.dhcp.from_, got.dhcp.to, got.access) == ("192.168.1.20", "192.168.1.99", "custom")
    assert router.other["dhcp"]["values"]["lan"]["limit"] == "80"
    # DHCP off: the section stays, ignored (as LuCI's "Ignore interface").
    devices = apply(cfg, change("network.set", {**lan, "dhcp": {"enabled": False}}))
    assert networks(devices)[1].dhcp.enabled is False
    assert router.other["dhcp"]["values"]["lan"]["ignore"] == "1"
    with pytest.raises(PluginError, match="the LAN cannot be removed"):
        apply(cfg, change("network.delete", {"vlan": 1}))


def test_the_network_omini_comes_through_keeps_the_web_interface(router, cfg):
    # Omini reaches the router at 192.168.1.1: the LAN.
    with pytest.raises(PluginError, match="would block the router's own web interface"):
        apply(
            cfg, change("network.set", {"vlan": 1, "gateway": "192.168.1.1/24", "access": "local"})
        )
    assert router.writes == []


def test_refusals(router, cfg):
    # A zone shared with another network: its rules are not Omini's to replace.
    zone = router.other["firewall"]["values"]["cfg02dc81"]
    zone["network"] = ["lan", "iot"]
    with pytest.raises(PluginError, match="shares the firewall zone lan with lan"):
        apply(
            cfg,
            change("network.set", {"vlan": 20, "gateway": "10.0.20.1/24", "access": "internet"}),
        )
    with pytest.raises(PluginError, match="discovery between VLANs is not managed on OpenWrt"):
        apply(cfg, change("network.set", {**CAMERAS, "mdns": True}))
    with pytest.raises(PluginError, match=r"not inside 192\.168\.30\.0/24"):
        apply(
            cfg,
            change(
                "network.set",
                {
                    **CAMERAS,
                    "dhcp": {"enabled": True, "from": "192.168.31.2", "to": "192.168.31.9"},
                },
            ),
        )
    with pytest.raises(PluginError, match="serves no network on VLAN 99"):
        apply(cfg, change("network.delete", {"vlan": 99}))
    assert router.writes == []
    # mDNS off is simply nothing to do.
    apply(cfg, change("network.set", {**CAMERAS, "mdns": False}))
    assert router.confirmed == 1


def test_a_bridge_without_vlan_filtering_is_left_alone(openwrt, cfg):
    openwrt.network["values"]["lan"].update(ipaddr="192.168.1.1/24")
    with pytest.raises(PluginError, match="bridge br-lan does not filter VLANs"):
        apply(cfg, change("network.set", CAMERAS))
    assert openwrt.writes == []


def test_rolled_back_when_the_router_does_not_serve_it(router, cfg, monkeypatch):
    monkeypatch.setattr(nets, "matches", lambda *_: False)
    with pytest.raises(PluginError, match=r"did not apply the network of VLAN 30 \(rolled back"):
        apply(cfg, change("network.set", CAMERAS))
    assert router.network == FILTERING
    assert router.other == {"dhcp": DHCP, "firewall": FIREWALL}
    assert router.confirmed == 0 and router.pending is None and router.staged_other == {}


def test_the_acl_must_allow_dhcp_and_firewall(router, cfg):
    router.uci_write_denied = True
    with pytest.raises(PluginError) as e:
        apply(cfg, change("network.set", CAMERAS))
    assert '"dhcp", "firewall"]' in str(e.value)
    router.denied.add("uci get")
    with pytest.raises(PluginError, match="does not allow reading the network config"):
        apply(cfg, change("network.set", CAMERAS))


def test_reservations(router, cfg):
    devices = apply(
        cfg,
        change(
            "reservation.set",
            {"mac": "AA-BB-CC-00-00-02", "ip": "192.168.1.60", "name": "Front Desk PC"},
        ),
    )
    assert [(r.mac, r.ip, r.name) for r in devices[0].dhcp_reservations] == [
        ("aa:bb:cc:00:00:02", "192.168.1.60", "front-desk-pc")
    ]
    devices = apply(
        cfg, change("reservation.set", {"mac": "aa:bb:cc:00:00:02", "ip": "192.168.1.61"})
    )
    assert [(r.ip, r.name) for r in devices[0].dhcp_reservations] == [("192.168.1.61", None)]
    hosts = [s for s in router.other["dhcp"]["values"].values() if s[".type"] == "host"]
    assert len(hosts) == 1
    devices = apply(cfg, change("reservation.delete", {"mac": "aa:bb:cc:00:00:02"}))
    assert devices[0].dhcp_reservations is None
    assert router.other["dhcp"] == DHCP and router.confirmed == 3
