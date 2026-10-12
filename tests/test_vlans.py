"""port.vlans, vlan.set and vlan.delete against the fake router's bridge-vlan
sections (DSA bridge VLAN filtering), shown back by netifd's status."""

import copy

import pytest
from omini_sdk import Change, PluginError

import omini_openwrt.collect as collect_module
from omini_openwrt.collect import apply, collect
from tests.conftest import sec

# /etc/config/network of a router whose bridge filters VLANs: VLAN 1 on lan1
# and lan2 (untagged) and lan3 (tagged), VLAN 20 untagged on lan3 — what the
# status fixture shows.
FILTERING = {
    "values": {
        "cfg030f15": sec(
            "cfg030f15", "device", name="br-lan", type="bridge", ports=["lan1", "lan2", "lan3"]
        ),
        "cfg050f15": sec(
            "cfg050f15",
            "bridge-vlan",
            device="br-lan",
            vlan="1",
            ports=["lan1:u*", "lan2:u*", "lan3:t"],
        ),
        "cfg060f15": sec("cfg060f15", "bridge-vlan", device="br-lan", vlan="20", ports=["lan3:u*"]),
        "lan": sec(
            "lan",
            "interface",
            False,
            device="br-lan.1",
            proto="static",
            ipaddr="192.168.1.1",
            netmask="255.255.255.0",
        ),
        "iot": sec(
            "iot", "interface", False, device="br-lan.20", proto="static", ipaddr="10.0.20.1/24"
        ),
        "wan": sec("wan", "interface", False, device="eth1", proto="dhcp"),
    }
}


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(collect_module, "VERIFY_S", 0)
    monkeypatch.setattr(collect_module, "VERIFY_PAUSE_S", 0)


@pytest.fixture
def router(openwrt):
    openwrt.network = copy.deepcopy(FILTERING)
    openwrt.reload()
    return openwrt


def change(kind, value, port=None):
    return Change(device="94:83:c4:00:00:01", kind=kind, port=port, value=value)


def membership(devices, port):
    pv = {i.name: i for i in devices[0].interfaces}[port].vlans
    if pv is None:
        return None
    return pv.untagged, [t.root for t in pv.tagged or []]


def ports_of(openwrt, vid):
    bv = next(
        s
        for s in openwrt.network["values"].values()
        if s[".type"] == "bridge-vlan" and s["vlan"] == str(vid)
    )
    return bv.get("ports")


def test_reads_the_vlans_and_their_names(router, cfg):
    router.network["values"]["cfg060f15"]["description"] = "IoT devices"
    [dev] = collect(cfg)
    assert [(v.id, v.name) for v in dev.vlans] == [(1, "lan"), (20, "IoT devices")]
    assert membership([dev], "lan3") == (20, [1])
    assert membership([dev], "eth1") is None  # not in a bridge: its VLANs cannot change


def test_an_access_port_moves_to_another_vlan(router, cfg):
    devices = apply(cfg, change("port.vlans", {"untagged": 20, "tagged": None}, "lan1"))
    assert [(m, a.get("section")) for m, a in router.writes] == [
        ("set", "cfg050f15"),
        ("set", "cfg060f15"),
        ("apply", None),
    ]
    assert ports_of(router, 1) == ["lan2:u*", "lan3:t"]  # the other ports as they were
    assert ports_of(router, 20) == ["lan3:u*", "lan1:u*"]
    assert router.confirmed == 1 and router.pending is None
    assert membership(devices, "lan1") == (20, [])
    assert membership(devices, "lan2") == (1, [])


def test_a_trunk_with_and_without_a_native_vlan(router, cfg):
    devices = apply(cfg, change("port.vlans", {"untagged": 1, "tagged": [20]}, "lan2"))
    assert membership(devices, "lan2") == (1, [20])
    assert ports_of(router, 20) == ["lan3:u*", "lan2:t"]
    devices = apply(cfg, change("port.vlans", {"untagged": None, "tagged": [1, 20]}, "lan2"))
    assert membership(devices, "lan2") == (None, [1, 20])
    assert ports_of(router, 1) == ["lan1:u*", "lan3:t", "lan2:t"]
    # Out of every VLAN: the option goes when no port is left in it.
    apply(cfg, change("port.vlans", {"untagged": 1, "tagged": None}, "lan3"))
    assert ports_of(router, 20) == ["lan2:t"]
    assert router.confirmed == 3


def test_the_same_membership_writes_nothing(router, cfg):
    apply(cfg, change("port.vlans", {"untagged": 20, "tagged": [1]}, "lan3"))
    assert router.writes == []


def test_refusals(router, cfg):
    with pytest.raises(PluginError, match="no VLAN 30 on br-lan: create it first"):
        apply(cfg, change("port.vlans", {"untagged": 30}, "lan1"))
    with pytest.raises(PluginError, match="eth1 is not a port of a bridge"):
        apply(cfg, change("port.vlans", {"untagged": 1}, "eth1"))
    with pytest.raises(PluginError, match="eth0 is the switch's CPU port"):
        apply(cfg, change("port.vlans", {"untagged": 1}, "eth0"))
    with pytest.raises(PluginError, match="br-lan is not a physical port"):
        apply(cfg, change("port.vlans", {"untagged": 1}, "br-lan"))
    assert router.writes == []


def test_a_bridge_without_vlan_filtering_is_left_alone(openwrt, cfg):
    # The default fake: br-lan has no bridge-vlan sections.
    for kind, value, port in (
        ("port.vlans", {"untagged": 20}, "lan1"),
        ("vlan.set", {"id": 30, "name": "Cameras"}, None),
    ):
        with pytest.raises(PluginError) as e:
            apply(cfg, change(kind, value, port))
        msg = str(e.value)
        assert "bridge br-lan does not filter VLANs" in msg
        assert "Network → Interfaces → Devices → br-lan" in msg
        assert "Enable VLAN filtering" in msg and "br-lan.1" in msg
    assert openwrt.writes == []


def test_creates_renames_and_deletes_a_vlan(router, cfg):
    devices = apply(cfg, change("vlan.set", {"id": 30, "name": "Cameras"}))
    added = router.writes[0]
    assert added == (
        "add",
        {
            "config": "network",
            "type": "bridge-vlan",
            "values": {"device": "br-lan", "vlan": "30", "description": "Cameras"},
        },
    )
    assert (30, "Cameras") in [(v.id, v.name) for v in devices[0].vlans]
    # No port is put in it: that is port.vlans' job.
    assert ports_of(router, 30) is None

    devices = apply(cfg, change("vlan.set", {"id": 30, "name": "CCTV"}))
    assert router.writes[2][0] == "set" and router.writes[2][1]["values"] == {"description": "CCTV"}
    assert (30, "CCTV") in [(v.id, v.name) for v in devices[0].vlans]
    n = len(router.writes)
    apply(cfg, change("vlan.set", {"id": 30, "name": "CCTV"}))
    assert len(router.writes) == n  # already so

    devices = apply(cfg, change("vlan.delete", {"id": 30}))
    assert 30 not in [v.id for v in devices[0].vlans]
    assert router.network == FILTERING
    assert router.confirmed == 3


def test_vlans_in_use_by_the_router_or_vlan_1_are_not_deleted(router, cfg):
    with pytest.raises(PluginError, match="interface iot uses VLAN 20: delete its network first"):
        apply(cfg, change("vlan.delete", {"id": 20}))
    with pytest.raises(PluginError, match="VLAN 1 cannot be deleted"):
        apply(cfg, change("vlan.delete", {"id": 1}))
    assert router.writes == []


def test_rolled_back_when_netifd_does_not_show_it(router, cfg):
    router.ignore_changes = True
    with pytest.raises(PluginError, match=r"did not apply the VLANs of lan1 \(rolled back"):
        apply(cfg, change("port.vlans", {"untagged": 20}, "lan1"))
    assert [m for m, _ in router.writes][-1] == "rollback"
    assert router.network == FILTERING and router.confirmed == 0 and router.pending is None
