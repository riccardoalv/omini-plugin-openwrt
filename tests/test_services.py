"""DNS names, pausing a device's internet, Wake-on-LAN, backups and LAGs."""

import copy

import pytest
from omini_sdk import Change, PluginError

import omini_openwrt.collect as collect_module
from omini_openwrt.collect import apply, backup, collect
from tests.conftest import DHCP, FIREWALL, sec


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(collect_module, "VERIFY_S", 0)
    monkeypatch.setattr(collect_module, "VERIFY_PAUSE_S", 0)


def change(kind, value):
    return Change(device="94:83:c4:00:00:01", kind=kind, value=value)


def domains(fake):
    return [
        (s["name"], s["ip"])
        for s in fake.other["dhcp"]["values"].values()
        if s[".type"] == "domain"
    ]


# --- DNS ----------------------------------------------------------------------------


def test_reads_the_local_domain_and_names(openwrt, cfg):
    openwrt.other["dhcp"]["values"]["cfg05"] = sec(
        "cfg05", "domain", name="NAS.lan", ip="192.168.1.20"
    )
    openwrt.other["dhcp"]["values"]["cfg06"] = sec("cfg06", "domain", name="v6.lan", ip="fd00::1")
    dev = collect(cfg)[0]
    assert dev.dns_domain == "lan"
    # A records only, lowercase.
    assert [(r.name, r.ip) for r in dev.dns_records] == [("nas.lan", "192.168.1.20")]


def test_sets_changes_and_deletes_a_name(openwrt, cfg):
    dev = apply(cfg, change("dns.set", {"name": "nas.lan", "ip": "192.168.1.20"}))[0]
    assert domains(openwrt) == [("nas.lan", "192.168.1.20")]
    assert [(r.name, r.ip) for r in dev.dns_records] == [("nas.lan", "192.168.1.20")]
    assert openwrt.confirmed == 1 and openwrt.pending is None
    # Same name, another address: the section is changed, not doubled.
    apply(cfg, change("dns.set", {"name": "nas.lan", "ip": "192.168.1.21"}))
    assert domains(openwrt) == [("nas.lan", "192.168.1.21")]
    # Already so: nothing is written.
    writes = len(openwrt.writes)
    apply(cfg, change("dns.set", {"name": "NAS.lan.", "ip": "192.168.1.21"}))
    assert len(openwrt.writes) == writes
    dev = apply(cfg, change("dns.delete", {"name": "nas.lan"}))[0]
    assert domains(openwrt) == [] and dev.dns_records is None
    assert openwrt.other["dhcp"] == DHCP


def test_a_duplicate_name_is_merged(openwrt, cfg):
    values = openwrt.other["dhcp"]["values"]
    values["cfg05"] = sec("cfg05", "domain", name="nas.lan", ip="192.168.1.20")
    values["cfg06"] = sec("cfg06", "domain", name="nas.lan", ip="192.168.1.30")
    apply(cfg, change("dns.set", {"name": "nas.lan", "ip": "192.168.1.30"}))
    assert domains(openwrt) == [("nas.lan", "192.168.1.30")]


def test_dns_refusals(openwrt, cfg):
    with pytest.raises(PluginError, match="not a DNS name"):
        apply(cfg, change("dns.set", {"name": "bad name", "ip": "192.168.1.20"}))
    with pytest.raises(PluginError, match="needs an IPv4 address"):
        apply(cfg, change("dns.set", {"name": "nas.lan", "ip": "fd00::1"}))
    assert openwrt.writes == []


def test_a_name_dnsmasq_does_not_keep_is_rolled_back(openwrt, cfg, monkeypatch):
    # The verification reads the config again: if it does not hold the name, rolled back.
    monkeypatch.setattr(collect_module.svc, "has_dns", lambda *_: False)
    with pytest.raises(PluginError, match=r"rolled back, not saved"):
        apply(cfg, change("dns.set", {"name": "nas.lan", "ip": "192.168.1.20"}))
    assert openwrt.other["dhcp"] == DHCP and openwrt.confirmed == 0


# --- pause ---------------------------------------------------------------------------

TABLET = "aa:bb:cc:00:00:01"


def pause_sections(fake):
    fw = fake.other["firewall"]["values"].values()
    sets = [s for s in fw if s[".type"] == "ipset"]
    rules = [s for s in fw if s[".type"] == "rule" and s.get("name") == "Omini pause"]
    return sets, rules


def test_pauses_and_resumes_a_device(openwrt, cfg):
    value = {"mac": TABLET.upper(), "ips": ["192.168.1.50", "fd00::50"], "name": "Kid tablet"}
    dev = apply(cfg, change("pause.set", value))[0]
    [ipset], [rule] = pause_sections(openwrt)
    assert ipset["name"] == "omini_paused" and ipset["match"] == ["src_ip"]
    assert ipset["entry"] == ["192.168.1.50"]  # IPv4 only
    assert ipset["omini"] == [f"{TABLET} 192.168.1.50 Kid tablet"]
    # One rule per zone but the WAN's: towards the WAN zone only (the LAN stays reachable).
    assert {k: rule[k] for k in ("src", "dest", "ipset", "target", "family")} == {
        "src": "lan",
        "dest": "wan",
        "ipset": "omini_paused",
        "target": "REJECT",
        "family": "ipv4",
    }
    assert [p.model_dump(exclude_none=True) for p in dev.paused] == [
        {"mac": TABLET, "ips": ["192.168.1.50"], "name": "Kid tablet"}
    ]
    assert openwrt.confirmed == 1

    # A second device, and the first one's new address (it changed IP).
    apply(cfg, change("pause.set", {"mac": "aa:bb:cc:00:00:02", "ips": ["192.168.1.60"]}))
    dev = apply(cfg, change("pause.set", {**value, "ips": ["192.168.1.51"]}))[0]
    [ipset], rules = pause_sections(openwrt)
    assert ipset["entry"] == ["192.168.1.51", "192.168.1.60"] and len(rules) == 1
    assert {p.mac: p.ips for p in dev.paused} == {
        TABLET: ["192.168.1.51"],
        "aa:bb:cc:00:00:02": ["192.168.1.60"],
    }

    apply(cfg, change("pause.delete", {"mac": TABLET}))
    [ipset], _ = pause_sections(openwrt)
    assert ipset["entry"] == ["192.168.1.60"]
    # The last one resumed: the set and the rules go away.
    dev = apply(cfg, change("pause.delete", {"mac": "aa:bb:cc:00:00:02"}))[0]
    assert pause_sections(openwrt) == ([], []) and dev.paused is None
    assert openwrt.other["firewall"] == FIREWALL
    # Resuming a device that is not paused changes nothing.
    writes = len(openwrt.writes)
    apply(cfg, change("pause.delete", {"mac": TABLET}))
    assert len(openwrt.writes) == writes


def test_a_rule_for_every_lan_zone(openwrt, cfg):
    values = openwrt.other["firewall"]["values"]
    values["cfg07"] = sec("cfg07", "zone", name="guest", network=["guest"], input="REJECT")
    apply(cfg, change("pause.set", {"mac": TABLET, "ips": ["192.168.1.50"]}))
    _, rules = pause_sections(openwrt)
    assert sorted(r["src"] for r in rules) == ["guest", "lan"]


def test_pause_refusals(openwrt, cfg):
    with pytest.raises(PluginError, match="is the router itself"):
        apply(cfg, change("pause.set", {"mac": TABLET, "ips": ["192.168.1.1"]}))
    with pytest.raises(PluginError, match="IPv4 address"):
        apply(cfg, change("pause.set", {"mac": TABLET, "ips": ["fd00::50"]}))
    with pytest.raises(PluginError, match="MAC address"):
        apply(cfg, change("pause.set", {"mac": "nope", "ips": ["192.168.1.50"]}))
    assert openwrt.writes == []


def test_a_pause_the_firewall_does_not_keep_is_rolled_back(openwrt, cfg, monkeypatch):
    monkeypatch.setattr(collect_module.svc, "pause_matches", lambda *_: False)
    with pytest.raises(PluginError, match=r"rolled back, not saved"):
        apply(cfg, change("pause.set", {"mac": TABLET, "ips": ["192.168.1.50"]}))
    assert openwrt.other["firewall"] == FIREWALL


# --- Wake-on-LAN ----------------------------------------------------------------------


def test_wakes_a_device_on_the_network_of_its_address(openwrt, cfg):
    apply(cfg, change("wol.send", {"mac": "AA:BB:CC:00:00:09", "ip": "10.0.20.9"}))
    apply(cfg, change("wol.send", {"mac": "aa:bb:cc:00:00:09"}))
    assert openwrt.execs == [
        {"command": "/usr/bin/etherwake", "params": ["-b", "-i", "br-lan.20", "aa:bb:cc:00:00:09"]},
        {"command": "/usr/bin/etherwake", "params": ["-b", "-i", "br-lan.1", "aa:bb:cc:00:00:09"]},
    ]
    assert openwrt.writes == []  # nothing in the configuration


def test_wol_refusals(openwrt, cfg):
    with pytest.raises(PluginError, match=r"holds 172\.16\.0\.9"):
        apply(cfg, change("wol.send", {"mac": "aa:bb:cc:00:00:09", "ip": "172.16.0.9"}))
    with pytest.raises(PluginError, match=r"holds 203\.0\.113\.9"):
        apply(cfg, change("wol.send", {"mac": "aa:bb:cc:00:00:09", "ip": "203.0.113.9"}))
    openwrt.etherwake = False
    with pytest.raises(PluginError, match="etherwake is not installed"):
        apply(cfg, change("wol.send", {"mac": "aa:bb:cc:00:00:09"}))
    openwrt.etherwake = True
    openwrt.denied |= {"file stat", "file exec"}
    with pytest.raises(PluginError, match=r'"/usr/bin/etherwake": \["list", "exec"\]'):
        apply(cfg, change("wol.send", {"mac": "aa:bb:cc:00:00:09"}))
    assert openwrt.execs == []


# --- backup ----------------------------------------------------------------------------

WIRELESS = {
    "values": {
        "radio0": sec("radio0", "wifi-device", False, type="mac80211", band="5g"),
        "default_radio0": sec(
            "default_radio0",
            "wifi-iface",
            False,
            device="radio0",
            mode="ap",
            ssid="Home",
            encryption="psk2",
            key="it's secret",
            network="lan",
        ),
    }
}


def test_backup_is_the_uci_export_without_secrets(openwrt, cfg):
    openwrt.other["wireless"] = copy.deepcopy(WIRELESS)
    [b] = backup(cfg)
    assert b.device == collect(cfg)[0].key and b.format == "text"
    text = b.content
    assert text.startswith("package network\n\nconfig device\n\toption name 'br-lan'\n")
    assert "\tlist ports 'lan1'\n" in text
    assert "config interface 'lan'\n\toption device 'br-lan'\n" in text
    for package in ("network", "wireless", "dhcp", "firewall", "system"):
        assert f"package {package}\n" in text
    assert text.index("package wireless") < text.index("package dhcp")
    assert "config wifi-iface 'default_radio0'\n\toption device 'radio0'" in text
    assert "secret" not in text and "option key" not in text
    assert "config timeserver 'ntp'\n\toption enabled '1'\n\tlist server" in text


def test_backup_is_deterministic(openwrt, cfg):
    first = backup(cfg)[0].content
    openwrt.routes[("system", "info")]["uptime"] += 600  # live status does not count
    assert backup(cfg)[0].content == first
    apply(cfg, change("dns.set", {"name": "nas.lan", "ip": "192.168.1.20"}))
    after = backup(cfg)[0].content
    assert after != first and "config domain\n\toption name 'nas.lan'\n" in after


def test_backup_follows_the_file_order(openwrt, cfg):
    values = openwrt.system["values"]
    values["ntp"][".index"], values["cfg01e48a"][".index"] = 0, 1
    text = backup(cfg)[0].content
    assert text.index("config timeserver") < text.index("config system")


def test_backup_without_any_readable_config(openwrt, cfg):
    openwrt.denied.add("uci get")
    with pytest.raises(PluginError, match="does not allow reading any uci config"):
        backup(cfg)


# --- LAGs ------------------------------------------------------------------------------


def test_bonding_members_report_their_lag(openwrt, cfg):
    values = openwrt.network["values"]
    values["cfg09"] = sec("cfg09", "device", name="bond0", type="bonding", ports=["lan1", "lan2"])
    status = openwrt.routes[("network.device", "status")]
    status["bond0"] = {"type": "bonding", "up": True, "carrier": True, "present": True}
    by = {i.name: i for i in collect(cfg)[0].interfaces}
    assert by["bond0"].type == "lag" and by["bond0"].members == ["lan1", "lan2"]
    assert by["lan1"].lag == "bond0" and by["lan2"].lag == "bond0"
    assert by["lan3"].lag is None


def test_proto_bonding_interfaces(openwrt, cfg):
    values = openwrt.network["values"]
    values["uplink"] = sec("uplink", "interface", False, proto="bonding", slaves=["lan3", "eth1"])
    by = {i.name: i for i in collect(cfg)[0].interfaces}
    assert by["lan3"].lag == "bonding-uplink" and by["eth1"].lag == "bonding-uplink"
