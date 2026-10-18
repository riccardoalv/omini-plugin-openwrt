import json

import httpx
import pytest
from omini_sdk import Config, PluginError

from omini_openwrt.collect import band_of, build, collect, norm_mac, parse_speed
from omini_openwrt.collect import test as connection_test


def test_reads_the_router(openwrt, cfg):
    [dev] = collect(cfg)
    assert dev.name == "gw-office"
    assert dev.key == "94:83:c4:00:00:01"  # MAC of the LAN, normalized
    assert dev.role == "router"
    assert dev.model == "GL.iNet GL-MT6000" and dev.vendor is None
    assert dev.os_version == "OpenWrt 23.05.3"
    assert dev.host == "192.168.1.1"
    assert dev.uptime_s == 273600
    assert dev.mem_pct == 25.0  # (total - available) / total
    assert dev.swap_pct is None  # no swap
    assert dev.load_avg == [0.5, 0.25, 0.1]  # divided by 65536
    assert dev.cpu_pct is None  # ubus has no CPU usage
    # No loopback and no link-local; the router's address in the LAN's prefix.
    assert dev.ips == [
        "192.168.1.1",
        "fd12:3456:789a::1",
        "10.0.20.1",
        "203.0.113.10",
        "2001:db8::10",
    ]
    assert [(s.mount, s.total_bytes, s.used_bytes) for s in dev.storage] == [
        ("/", 7168 * 1024, 2048 * 1024),
        ("/tmp", 524288 * 1024, 4096 * 1024),
    ]
    [t] = dev.temperatures
    assert (t.sensor, t.kind, t.celsius) == ("cpu-thermal", "cpu", 52.3)


def test_ports_with_speed_traffic_and_bridges(openwrt, cfg):
    [dev] = collect(cfg)
    ports = {i.name: i for i in dev.interfaces}
    assert "lo" not in ports and "lan4" not in ports  # loopback, absent port
    wan = ports["eth1"]
    assert (wan.type, wan.up, wan.speed_mbps, wan.duplex) == ("ethernet", True, 1000, "full")
    assert (wan.rx_bytes, wan.tx_bytes, wan.rx_errors) == (98765432100, 12345678900, 3)
    assert wan.wan is True and wan.description == "wan, wan6"
    assert wan.ips == ["203.0.113.10/24", "2001:db8::10/64"]  # link-local left out
    assert ports["lan1"].type == "ethernet" and ports["lan1"].speed_mbps == 1000
    assert (ports["lan2"].up, ports["lan2"].speed_mbps) == (False, None)  # no carrier
    assert (ports["lan3"].speed_mbps, ports["lan3"].duplex) == (100, "half")
    br = ports["br-lan"]
    assert br.type == "bridge" and br.members == ["lan1", "lan2", "lan3", "phy0-ap0", "phy1-ap0"]
    vlan = ports["br-lan.20"]
    assert (vlan.type, vlan.parent, vlan.vlan, vlan.description) == ("vlan", "br-lan", 20, "iot")
    assert ports["phy0-ap0"].type == "wireless"
    assert ports["eth1"].mac == "94:83:c4:00:00:02"


def test_bridge_vlans(openwrt, cfg):
    [dev] = collect(cfg)
    ports = {i.name: i for i in dev.interfaces}
    assert (ports["lan1"].vlans.untagged, ports["lan1"].vlans.tagged) == (1, None)
    assert ports["lan3"].vlans.untagged == 20
    assert [t.root for t in ports["lan3"].vlans.tagged] == [1]
    assert ports["eth1"].vlans is None
    assert [(v.id, v.name, v.interface, v.subnet) for v in dev.vlans] == [
        (1, "lan", "br-lan.1", "192.168.1.0/24"),
        (20, "iot", "br-lan.20", "10.0.20.0/24"),
    ]


def test_wan_gateways(openwrt, cfg):
    [dev] = collect(cfg)
    assert [(g.name, g.interface, g.address, g.status) for g in dev.gateways] == [
        ("wan", "eth1", "203.0.113.1", "up"),
        ("wan6", "eth1", "fe80::1", "up"),
    ]


def test_pppoe_wan_runs_on_its_port(openwrt, cfg):
    dump = openwrt.routes[("network.interface", "dump")]["interface"]
    wan = next(i for i in dump if i["interface"] == "wan")
    wan.update(proto="pppoe", l3_device="pppoe-wan", device="eth1")
    [dev] = collect(cfg)
    ports = {i.name: i for i in dev.interfaces}
    ppp = ports["pppoe-wan"]
    assert (ppp.type, ppp.wan, ppp.parent, ppp.up) == ("tunnel", True, "eth1", True)
    assert ppp.ips == ["203.0.113.10/24"]


def test_a_down_wan_still_makes_a_router(openwrt, cfg):
    dump = openwrt.routes[("network.interface", "dump")]["interface"]
    dump[:] = [i for i in dump if i["interface"] != "wan6"]
    wan = next(i for i in dump if i["interface"] == "wan")
    wan.update(up=False, route=[], **{"ipv4-address": []})
    [dev] = collect(cfg)
    assert dev.role == "router"
    assert [(g.name, g.status, g.address) for g in dev.gateways] == [("wan", "down", None)]


def test_access_point_without_wan(openwrt, cfg):
    dump = openwrt.routes[("network.interface", "dump")]["interface"]
    dump[:] = [i for i in dump if not i["interface"].startswith("wan")]
    [dev] = collect(cfg)
    assert dev.role == "ap" and dev.gateways is None
    assert not any(i.wan for i in dev.interfaces)


def test_wifi_clients(openwrt, cfg):
    [dev] = collect(cfg)
    clients = {c.mac: c for c in dev.wireless_clients}
    assert set(clients) == {"aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:05"}
    phone = clients["aa:bb:cc:dd:ee:02"]
    assert (phone.interface, phone.ssid, phone.band) == ("Office · 2.4 GHz", "Office", "2.4ghz")
    assert (phone.signal_dbm, phone.tx_rate_mbps, phone.rx_rate_mbps) == (-61, 144.4, 72.2)
    laptop = clients["aa:bb:cc:dd:ee:01"]
    assert (laptop.band, laptop.tx_rate_mbps, laptop.rx_rate_mbps) == ("5ghz", 1134.2, 1201.0)
    odd = clients["aa:bb:cc:dd:ee:05"]  # no signal or rate reported: unknown, not zero
    assert (odd.signal_dbm, odd.tx_rate_mbps, odd.rx_rate_mbps) == (None, None, None)
    # The upstream AP of a client-mode interface is not a client.
    assert ("iwinfo", "assoclist") in openwrt.calls
    assert "11:22:33:44:55:66" not in clients


def test_arp_leases_and_hosts(openwrt, cfg):
    [dev] = collect(cfg)
    assert [(a.ip, a.mac, a.interface) for a in dev.arp] == [
        ("192.168.1.50", "aa:bb:cc:dd:ee:01", "br-lan.1"),  # incomplete entry skipped
        ("203.0.113.1", "00:11:22:33:44:55", "eth1"),
    ]
    assert [(lease.ip, lease.mac, lease.hostname) for lease in dev.dhcp_leases] == [
        ("192.168.1.50", "aa:bb:cc:dd:ee:01", "laptop"),
        ("192.168.1.60", "aa:bb:cc:dd:ee:02", "phone"),
        ("192.168.1.70", "aa:bb:cc:dd:ee:04", None),
        # DHCPv6 (odhcpd): the MAC from macaddr or from a DUID-LLT / DUID-LL; one
        # whose DUID carries no MAC (DUID-UUID) is left out.
        ("fd12:3456:789a::5c", "aa:bb:cc:dd:ee:01", "laptop"),
        ("2001:db8:1::5c", "aa:bb:cc:dd:ee:01", "laptop"),
        ("fd12:3456:789a::20", "aa:bb:cc:dd:ee:05", "nas"),
    ]
    assert dev.dhcp_leases[0].expires_at is not None
    assert dev.dhcp_leases[2].expires_at is None  # unknown / infinite
    assert [(h.ip, h.mac, h.hostnames, h.sources) for h in dev.hosts] == [
        ("192.168.1.50", "aa:bb:cc:dd:ee:01", ["laptop"], ["openwrt"]),
        ("192.168.1.60", "aa:bb:cc:dd:ee:02", ["phone"], ["openwrt"]),  # name from DHCP
    ]


def test_optional_methods_refused_by_the_acl_are_skipped(openwrt, cfg):
    openwrt.denied = {"iwinfo devices", "luci-rpc getDHCPLeases", "file read", "file list"}
    [dev] = collect(cfg)
    assert dev.wireless_clients is None and dev.dhcp_leases is None and dev.arp is None
    assert dev.temperatures is None
    assert dev.interfaces and dev.hosts  # the rest still works
    msg = connection_test(cfg)
    assert msg.startswith("Connected to gw-office (OpenWrt 23.05.3). Not allowed by the ACL:")
    assert "iwinfo devices" in msg and "luci-rpc getDHCPLeases" in msg
    assert "file read /proc/net/arp" in msg
    assert "thermal" not in msg  # temperatures are an extra: never reported


def test_missing_objects_are_skipped(openwrt, cfg):
    # No LuCI (luci-rpc) and no wireless (iwinfo): an x86 router without Wi-Fi.
    for key in list(openwrt.routes):
        if key[0] in ("luci-rpc", "iwinfo"):
            del openwrt.routes[key]
    [dev] = collect(cfg)
    assert dev.hosts is None and dev.wireless_clients is None and dev.interfaces
    assert connection_test(cfg) == "Connected to gw-office (OpenWrt 23.05.3)."


def test_connection_test(openwrt, cfg):
    assert connection_test(cfg) == "Connected to gw-office (OpenWrt 23.05.3)."


def test_wrong_credentials(openwrt, cfg):
    cfg["password"] = "wrong"
    with pytest.raises(PluginError, match="rejected the username or password"):
        collect(cfg)


def test_required_method_refused(openwrt, cfg):
    openwrt.denied = {"system board"}
    with pytest.raises(PluginError, match="does not allow ubus system board"):
        collect(cfg)


def test_not_openwrt(cfg, monkeypatch):
    import omini_openwrt.collect as m
    from omini_openwrt.client import Client

    def handler(request):
        return httpx.Response(404, text="nothing here")

    monkeypatch.setattr(
        m, "Client", lambda *a, **k: Client(*a, **k, transport=httpx.MockTransport(handler))
    )
    with pytest.raises(PluginError, match="no /ubus endpoint"):
        collect(cfg)


def test_unreachable(cfg, monkeypatch):
    import omini_openwrt.collect as m
    from omini_openwrt.client import Client

    def handler(request):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        m, "Client", lambda *a, **k: Client(*a, **k, transport=httpx.MockTransport(handler))
    )
    with pytest.raises(PluginError, match="cannot connect"):
        collect(cfg)


def test_session_is_kept_and_renewed(openwrt, cfg):
    collect(cfg)
    collect(cfg)
    assert openwrt.logins == 1  # the saved session was reused
    saved = json.loads((cfg.state_dir / "session.json").read_text())
    assert saved["username"] == "omini" and "secret" not in json.dumps(saved)
    openwrt.expire_sessions()  # 5 minutes without use
    [dev] = collect(cfg)
    assert openwrt.logins == 2 and dev.name == "gw-office"


def test_without_state_dir(openwrt):
    cfg = Config({"url": "192.168.1.1", "username": "omini", "password": "secret"})
    [dev] = collect(cfg)
    assert dev.host == "192.168.1.1"


def test_required_fields():
    with pytest.raises(PluginError, match="required"):
        collect(Config({"url": "https://192.168.1.1"}))


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff"),
        ("aa-bb-cc-dd-ee-ff", "aa:bb:cc:dd:ee:ff"),
        (" 00:11:22:33:44:55 ", "00:11:22:33:44:55"),
        ("00:00:00:00:00:00", None),
        ("not a mac", None),
        (None, None),
    ],
)
def test_mac_normalization(raw, want):
    assert norm_mac(raw) == want


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("1000F", (1000, "full")),
        ("10H", (10, "half")),
        ("2500", (2500, None)),
        ("-1", (None, None)),
        ("-1F", (None, None)),
        (None, (None, None)),
    ],
)
def test_speed(raw, want):
    assert parse_speed(raw) == want


@pytest.mark.parametrize(
    ("info", "want"),
    [
        ({"frequency": 2412}, "2.4ghz"),
        ({"frequency": 5745}, "5ghz"),
        ({"frequency": 5955}, "6ghz"),
        ({"hwmodes": ["b", "g", "n"]}, "2.4ghz"),
        ({"hwmodes": ["a", "n", "ac"]}, "5ghz"),
        ({"hwmodes": ["a", "b", "g", "n"]}, None),  # dual band radio: unknown
        ({}, None),
    ],
)
def test_band(info, want):
    assert band_of(info) == want


def bare(board):
    """A device that answered system board only."""
    raw = dict.fromkeys(("info", "devices", "interfaces", "hints", "leases", "arp", "config"))
    return {**raw, "board": board, "wifi": [], "temps": []}


def test_the_key_without_macs_or_address():
    assert build(bare({"hostname": "ap-hall"}), None).key == "ap-hall"
    assert build(bare({"hostname": "ap-hall"}), "192.168.1.2").key == "192.168.1.2"
    with pytest.raises(PluginError, match="nothing to tell this device apart"):
        build(bare({}), None)  # never the literal "None"


@pytest.mark.parametrize("firewall", [True, False])
def test_a_dumb_ap_with_a_gateway_on_its_lan_is_no_router(openwrt, cfg, firewall):
    dump = openwrt.routes[("network.interface", "dump")]["interface"]
    dump[:] = [i for i in dump if not i["interface"].startswith("wan")]
    lan = next(i for i in dump if i["interface"] == "lan")
    lan["route"] = [{"target": "0.0.0.0", "mask": 0, "nexthop": "192.168.1.254"}]
    if not firewall:
        openwrt.denied.add("uci get")  # without the zones: OpenWrt's "lan" name
    [dev] = collect(cfg)
    assert dev.role == "ap" and dev.gateways is None
    assert not any(i.wan for i in dev.interfaces)


def test_a_default_route_in_the_wan_zone_is_a_wan(openwrt, cfg):
    # The uplink is named "lan" but sits in the masquerading zone: a router.
    dump = openwrt.routes[("network.interface", "dump")]["interface"]
    dump[:] = [i for i in dump if not i["interface"].startswith("wan")]
    lan = next(i for i in dump if i["interface"] == "lan")
    lan["route"] = [{"target": "0.0.0.0", "mask": 0, "nexthop": "192.168.1.254"}]
    zones = openwrt.other["firewall"]["values"]
    zones["cfg02dc81"]["network"] = []
    zones["cfg03dc81"]["network"] = ["lan"]
    [dev] = collect(cfg)
    assert dev.role == "router" and [g.name for g in dev.gateways] == ["lan"]


def test_an_8021q_device_reports_its_vid(openwrt, cfg):
    # A VLAN device named in LuCI (not <port>.<vid>): its VLAN is its vid.
    openwrt.routes[("network.device", "status")]["cams"] = {
        "present": True,
        "type": "8021q",
        "devtype": "vlan",
        "up": True,
    }
    openwrt.network["values"]["cfg090f15"] = {
        ".anonymous": True,
        ".type": "device",
        ".name": "cfg090f15",
        "name": "cams",
        "type": "8021q",
        "ifname": "eth1",
        "vid": "40",
    }
    [dev] = collect(cfg)
    cams = next(i for i in dev.interfaces if i.name == "cams")
    assert (cams.type, cams.vlan, cams.parent) == ("vlan", 40, "eth1")


def test_memory_in_bytes_and_ipv6_neighbors(openwrt, cfg):
    [dev] = collect(cfg)
    assert (dev.mem_total_bytes, dev.mem_used_bytes) == (1073741824, 1073741824 - 805306368)
    # getHostHints' ip6addrs: which MAC each IPv6 address belongs to.
    assert [(n.ip, n.mac) for n in dev.ndp] == [
        ("fe80::1", "aa:bb:cc:dd:ee:01"),
        ("fd12:3456:789a::3", "aa:bb:cc:dd:ee:03"),
    ]


def test_the_mac_of_a_duid():
    from omini_openwrt.collect import mac_of_duid

    assert mac_of_duid("00:01:00:01:29:8a:7c:10:aa:bb:cc:dd:ee:01") == "aa:bb:cc:dd:ee:01"
    assert mac_of_duid("00030001AABBCCDDEE05") == "aa:bb:cc:dd:ee:05"
    assert mac_of_duid("0002000000090c4c0123456789") is None  # DUID-EN: no MAC
    assert mac_of_duid("00010006298a7c10aabbccddee01") is None  # not Ethernet
    assert mac_of_duid(None) is None
