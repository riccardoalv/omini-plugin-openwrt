"""Wi-Fi radios, their channels (wifi.channel) and a wireless uplink."""

import copy

import pytest
from omini_sdk import Change, PluginError

import omini_openwrt.collect as collect_module
from omini_openwrt import radios
from omini_openwrt.collect import apply, collect
from tests.conftest import sec

# /etc/config/wireless of a dual-band router repeating another access point
# on 5 GHz (radio1's `wwan`, mode sta).
WIRELESS = {
    "values": {
        "radio0": sec(
            "radio0",
            "wifi-device",
            False,
            type="mac80211",
            path="platform/soc/18000000.wifi",
            band="2g",
            channel="6",
            htmode="HE20",
            country="US",
        ),
        "radio1": sec(
            "radio1",
            "wifi-device",
            False,
            type="mac80211",
            path="platform/soc/18000000.wifi+1",
            band="5g",
            channel="auto",
            htmode="HE80",
        ),
        "default_radio0": sec(
            "default_radio0",
            "wifi-iface",
            False,
            device="radio0",
            mode="ap",
            ssid="Office",
            network="lan",
            encryption="sae-mixed",
        ),
        "default_radio1": sec(
            "default_radio1",
            "wifi-iface",
            False,
            device="radio1",
            mode="ap",
            ssid="Office",
            network="lan",
            encryption="sae-mixed",
        ),
        "wwan": sec(
            "wwan",
            "wifi-iface",
            False,
            device="radio1",
            mode="sta",
            ssid="Upstream",
            network="wwan",
        ),
    }
}


@pytest.fixture
def router(openwrt, monkeypatch):
    monkeypatch.setattr(collect_module, "VERIFY_S", 0)
    monkeypatch.setattr(collect_module, "VERIFY_PAUSE_S", 0)
    openwrt.other["wireless"] = copy.deepcopy(WIRELESS)
    real = openwrt.reload

    def reload():
        # The Wi-Fi restarts on the saved channel: iwinfo shows it on the radio's interfaces.
        real()
        if openwrt.ignore_changes:
            return
        ch = openwrt.other["wireless"]["values"]["radio0"]["channel"]
        info = openwrt.routes[("iwinfo", "info")]["phy0-ap0"]
        info["channel"] = int(ch)
        info["frequency"] = 2407 + 5 * int(ch)

    openwrt.reload = reload
    return openwrt


def channel(radio="radio0", ch=11):
    return Change(
        device="94:83:c4:00:00:01", kind="wifi.channel", value={"radio": radio, "channel": ch}
    )


def test_reads_each_radio_and_its_channel(router, cfg):
    dev = collect(cfg)[0]
    got = {r.name: r for r in dev.radios}
    r0, r1 = got["radio0"], got["radio1"]
    assert (r0.band, r0.channel, r0.width_mhz, r0.auto, r0.tx_power_dbm, r0.enabled) == (
        "2.4ghz",
        6,
        20,
        False,
        20.0,
        True,
    )
    # On auto, the channel is the one iwinfo shows now.
    assert (r1.band, r1.channel, r1.width_mhz, r1.auto) == ("5ghz", 36, 80, True)
    assert dev.wireless_uplink is True  # the sta interface


def test_no_client_interface_is_no_wireless_uplink(router, cfg):
    values = router.other["wireless"]["values"]
    values["wwan"]["disabled"] = "1"
    values["mesh"] = sec("mesh", "wifi-iface", False, device="radio0", mode="mesh", disabled="1")
    assert collect(cfg)[0].wireless_uplink is False
    values["mesh"].pop("disabled")
    assert collect(cfg)[0].wireless_uplink is True


def test_without_the_wireless_config_iwinfo_tells_the_uplink(openwrt, cfg):
    dev = collect(cfg)[0]
    assert dev.radios is None
    assert dev.wireless_uplink is True  # phy1-sta0 is in Client mode


def test_without_netifd_status_iwinfo_is_asked_for_the_radio(router, cfg):
    router.denied.add("network.wireless status")
    router.routes[("iwinfo", "info")]["radio0"] = {"channel": 1, "txpower": 17, "mode": "Master"}
    got = {r.name: r for r in collect(cfg)[0].radios}
    assert (got["radio0"].channel, got["radio0"].tx_power_dbm) == (1, 17.0)
    assert got["radio1"].channel is None and got["radio1"].auto is True


def test_widths_and_bands():
    assert [radios.width_of(m) for m in ("HT40", "VHT160", "EHT320", "NOHT", "VHT80+80")] == [
        40,
        160,
        320,
        20,
        None,
    ]
    old = {"values": {"r": sec("r", "wifi-device", False, hwmode="11a", channel="0")}}
    [r] = radios.radios(old, None, {})
    assert (r["band"], r["channel"], r["auto"]) == ("5ghz", None, True)


def test_moves_a_radio_to_another_channel(router, cfg):
    dev = apply(cfg, channel(ch=11))[0]
    assert router.other["wireless"]["values"]["radio0"]["channel"] == "11"
    assert router.confirmed == 1 and router.pending is None
    assert {r.name: r.channel for r in dev.radios}["radio0"] == 11
    # Nothing else in the radio's section changed.
    assert {k: v for k, v in router.other["wireless"]["values"]["radio0"].items()} == {
        **WIRELESS["values"]["radio0"],
        "channel": "11",
    }
    # Already there: nothing is written.
    writes = len(router.writes)
    apply(cfg, channel(ch=11))
    assert len(router.writes) == writes


def test_rolled_back_unless_the_radio_runs_on_it(router, cfg):
    router.ignore_changes = True  # the Wi-Fi did not restart on the new channel
    with pytest.raises(PluginError, match=r"rolled back, not saved"):
        apply(cfg, channel(ch=1))
    assert router.other["wireless"] == WIRELESS and router.confirmed == 0


def test_refusals(router, cfg):
    with pytest.raises(PluginError, match="no Wi-Fi radio radio9"):
        apply(cfg, channel("radio9"))
    with pytest.raises(PluginError, match="not a channel"):
        apply(cfg, channel(ch=36))  # a 5 GHz channel on the 2.4 GHz radio
    router.other["wireless"]["values"]["radio0"]["disabled"] = "1"
    with pytest.raises(PluginError, match="is off"):
        apply(cfg, channel())
    with pytest.raises(PluginError, match=r"wifi\.channel needs"):
        apply(cfg, Change(device="x", kind="wifi.channel", value={"radio": "radio0"}))
    assert router.writes == []
