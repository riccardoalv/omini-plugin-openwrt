from omini_openwrt import forwards
from omini_openwrt.collect import collect


def sec(section, kind, **options):
    return {".anonymous": True, ".type": kind, ".name": section, **options}


# /etc/config/firewall redirects as LuCI's Port Forwards page saves them.
REDIRECTS = {
    "cfg10": sec(
        "cfg10",
        "redirect",
        name="NAS web",
        target="DNAT",
        src="wan",
        src_dport="8443",
        dest="lan",
        dest_ip="192.168.1.20",
        dest_port="443",
        proto="tcp",
    ),
    # No proto (fw4: tcp udp), no dest_port (the same port), a range, off.
    "cfg11": sec(
        "cfg11",
        "redirect",
        name="Game",
        src="wan",
        src_dport="27015-27030",
        dest_ip="192.168.1.30",
        enabled="0",
    ),
    "cfg12": sec(
        "cfg12",
        "redirect",
        name="SSH from the office",
        target="DNAT",
        src="wan",
        src_dport="2222",
        src_ip="198.51.100.0/24",
        dest_ip="192.168.1.10",
        dest_port="22",
        proto=["tcp", "udp"],
    ),
    # IPv6 DNAT (fw4 does it), and a source NAT, which is no port forward.
    "cfg13": sec(
        "cfg13",
        "redirect",
        target="DNAT",
        src="wan",
        src_dport="80",
        dest_ip="fd12:3456:789a::20",
        proto="tcp",
        family="ipv6",
    ),
    "cfg14": sec("cfg14", "redirect", target="SNAT", src="lan", src_dip="203.0.113.10"),
    "cfg15": sec("cfg15", "redirect", target="DNAT", src="wan", proto="icmp"),
}

# miniupnpd's lease file: with a timestamp (2.x) and without (1.x).
LEASES = (
    "TCP:51413:192.168.1.50:51413:1760000000:Transmission at 51413\n"
    "UDP:3074:192.168.1.60:3074:Xbox:Live\n"
    "garbage\n"
)


def test_redirects_are_port_forwards():
    got = forwards.redirects({"values": REDIRECTS})
    assert got[0] == {
        "protocol": "tcp",
        "external_port": 8443,
        "external_port_end": None,
        "internal_ip": "192.168.1.20",
        "internal_port": 443,
        "source": None,
        "description": "NAS web",
        "enabled": True,
        "upnp": False,
    }
    game = got[1]
    assert (game["protocol"], game["external_port"], game["external_port_end"]) == (
        "tcp/udp",
        27015,
        27030,
    )
    assert game["internal_port"] == 27015 and game["enabled"] is False
    assert got[2]["source"] == "198.51.100.0/24" and got[2]["protocol"] == "tcp/udp"
    assert got[3]["internal_ip"] == "fd12:3456:789a::20"
    assert len(got) == 4  # neither SNAT nor ICMP forwards a port


def test_upnp_leases_and_luci_status():
    got = forwards.upnp_leases(LEASES)
    assert [(f["protocol"], f["external_port"], f["internal_ip"]) for f in got] == [
        ("tcp", 51413, "192.168.1.50"),
        ("udp", 3074, "192.168.1.60"),
    ]
    assert got[0]["description"] == "Transmission at 51413"
    assert got[1]["description"] == "Xbox:Live"  # no timestamp: the rest is the description
    assert all(f["upnp"] for f in got)
    status = {
        "rules": [
            {
                "num": "1",
                "proto": "UDP",
                "extport": "500",
                "intaddr": "192.168.1.70",
                "host_hint": "phone",
                "intport": "500",
                "expires": 3600,
                "description": "WhatsApp",
            }
        ]
    }
    assert forwards.upnp_status(status)[0]["internal_port"] == 500
    assert forwards.upnp_status({}) is None


def test_collect_reports_the_forwards(openwrt, cfg):
    openwrt.other["firewall"]["values"].update(REDIRECTS)
    openwrt.routes[("file", "read")][forwards.LEASE_FILE] = LEASES
    dev = collect(cfg)[0]
    fw = dev.port_forwards
    assert [f.upnp for f in fw] == [False] * 4 + [True] * 2
    assert fw[0].external_port == 8443 and fw[0].internal_port == 443


def test_without_upnp_or_redirects_nothing_is_reported(openwrt, cfg):
    dev = collect(cfg)[0]
    assert dev.port_forwards is None


def test_a_refused_upnp_read_is_not_listed(openwrt, cfg):
    from omini_openwrt.collect import test

    openwrt.denied |= {"luci.upnp get_status", "file read"}
    assert collect(cfg)[0].port_forwards is None
    assert "upnp" not in test(cfg)
