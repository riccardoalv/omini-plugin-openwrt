import copy

import pytest
from omini_sdk import Change, PluginError

import omini_openwrt.collect as collect_module
from omini_openwrt.collect import apply, collect
from tests.conftest import NETWORK


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(collect_module, "VERIFY_S", 0)
    monkeypatch.setattr(collect_module, "VERIFY_PAUSE_S", 0)


def change(port="lan1", value=False, kind="port.enabled"):
    return Change(device="94:83:c4:00:00:01", kind=kind, port=port, value=value)


def ports(devices):
    return {i.name: i for i in devices[0].interfaces}


def test_reads_whether_each_port_is_enabled(openwrt, cfg):
    openwrt.network["values"]["cfg040f15"]["enabled"] = "0"
    openwrt.running = {"lan3"}
    by = ports(collect(cfg))
    assert by["lan1"].enabled is True and by["eth1"].enabled is True
    # Disabled: netifd leaves it out of its status; it comes from the configuration.
    assert (by["lan3"].enabled, by["lan3"].up, by["lan3"].type) == (False, False, "ethernet")
    # The DSA switch's CPU port, bridges, VLANs and Wi-Fi are not ports to turn off.
    for name in ("eth0", "br-lan", "br-lan.1", "phy0-ap0"):
        assert by[name].enabled is None


def test_without_the_uci_read_the_present_ports_are_enabled(openwrt, cfg):
    openwrt.denied.add("uci get")
    by = ports(collect(cfg))
    assert by["lan1"].enabled is True and by["eth0"].enabled is None


def test_disables_a_port_keeping_the_other_settings(openwrt, cfg):
    devices = apply(cfg, change())
    # A device section of its own for lan1: the bridge and lan3's section stay as they were.
    assert [m for m, _ in openwrt.writes] == ["add", "apply"]
    assert openwrt.writes[0][1] == {
        "config": "network",
        "type": "device",
        "values": {"name": "lan1", "enabled": "0"},
    }
    assert openwrt.writes[1][1] == {"rollback": True, "timeout": 30}
    values = openwrt.network["values"]
    assert {k: v for k, v in values.items() if k in NETWORK["values"]} == NETWORK["values"]
    assert openwrt.confirmed == 1 and openwrt.pending is None
    by = ports(devices)
    assert (by["lan1"].enabled, by["lan1"].up) == (False, False)
    assert by["lan2"].enabled is True


def test_a_port_with_a_section_keeps_its_options(openwrt, cfg):
    apply(cfg, change("lan3"))
    assert openwrt.writes[0] == (
        "set",
        {"config": "network", "section": "cfg040f15", "values": {"enabled": "0"}},
    )
    lan3 = openwrt.network["values"]["cfg040f15"]
    assert lan3 == {**NETWORK["values"]["cfg040f15"], "enabled": "0"}
    # Enabling removes the option again: back to the file as it was.
    devices = apply(cfg, change("lan3", True))
    assert openwrt.writes[2] == (
        "delete",
        {"config": "network", "section": "cfg040f15", "option": "enabled"},
    )
    assert openwrt.network == NETWORK
    assert openwrt.confirmed == 2 and ports(devices)["lan3"].enabled is True


def test_rolled_back_and_not_saved_when_the_port_did_not_change(openwrt, cfg):
    openwrt.ignore_changes = True
    with pytest.raises(PluginError, match=r"did not disable lan1 \(rolled back, not saved\)"):
        apply(cfg, change())
    assert [m for m, _ in openwrt.writes] == ["add", "apply", "rollback"]
    assert openwrt.network == NETWORK and openwrt.confirmed == 0
    assert openwrt.pending is None and openwrt.staged == {}


def test_nothing_is_sent_when_the_port_is_already_so(openwrt, cfg):
    apply(cfg, change(value=True))
    openwrt.network["values"]["cfg040f15"]["enabled"] = "0"
    apply(cfg, change("lan3", False))
    assert openwrt.writes == [] and openwrt.confirmed == 0


def test_unknown_ports_and_changes(openwrt, cfg):
    with pytest.raises(PluginError, match="no port lan9"):
        apply(cfg, change("lan9"))
    with pytest.raises(PluginError, match="br-lan is not a physical port"):
        apply(cfg, change("br-lan"))
    with pytest.raises(PluginError, match="eth0 is the switch's CPU port"):
        apply(cfg, change("eth0"))
    with pytest.raises(PluginError, match="cannot apply"):
        apply(cfg, change(value="off"))
    assert openwrt.writes == []


def test_swconfig_switches_are_refused(openwrt, cfg):
    openwrt.network["values"]["cfg010f15"] = {
        ".anonymous": True,
        ".type": "switch",
        ".name": "cfg010f15",
        "name": "switch0",
    }
    with pytest.raises(PluginError, match="swconfig"):
        apply(cfg, change())
    assert openwrt.writes == []


def test_a_read_only_acl_names_what_it_lacks(openwrt, cfg):
    # The ubus methods are not in the ACL file...
    openwrt.denied |= {"uci revert", "uci add"}
    with pytest.raises(PluginError) as e:
        apply(cfg, change())
    msg = str(e.value)
    assert "the ACL of user 'omini' does not allow changes (ubus uci revert)" in msg
    assert '"uci": ["network", "wireless"]' in msg and "list write 'omini'" in msg
    # ... or they are, but the login has no write access to the network config.
    openwrt.denied.clear()
    openwrt.uci_write_denied = True
    with pytest.raises(PluginError, match="does not allow changes"):
        apply(cfg, change())
    # Without even uci get, the read part is named.
    openwrt.denied.add("uci get")
    with pytest.raises(PluginError, match=r'"read": \{"ubus": \{"uci": \["get"\]\}'):
        apply(cfg, change())
    assert openwrt.network == NETWORK and openwrt.writes == []


def test_another_pending_apply_changes_nothing(openwrt, cfg):
    openwrt.other_pending = True
    with pytest.raises(PluginError, match="another change waiting to be confirmed"):
        apply(cfg, change())
    assert openwrt.network == NETWORK and openwrt.staged == {}


def test_staged_leftovers_are_dropped_first(openwrt, cfg):
    sid = f"{openwrt.logins + 1:032x}"  # the session apply logs in with
    openwrt.staged[sid] = copy.deepcopy(NETWORK)
    openwrt.staged[sid]["values"]["lan"]["proto"] = "dhcp"
    apply(cfg, change())
    assert openwrt.network["values"]["lan"]["proto"] == "static"
