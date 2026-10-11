import copy
import json
from functools import partial
from pathlib import Path

import httpx
import pytest

import omini_openwrt.collect as collect_module
from omini_openwrt.client import Client

FIXTURES = Path(__file__).parent / "fixtures"

# Calls whose fixture is a map from one argument to the answer.
KEYED = {
    ("iwinfo", "info"): "device",
    ("iwinfo", "assoclist"): "device",
    ("file", "read"): "path",
    ("file", "list"): "path",
}


# /etc/config/network of a DSA router (the device sections; interfaces are
# left out): lan3 has a section of its own (a MAC set in LuCI).
NETWORK = {
    "values": {
        "cfg030f15": {
            ".anonymous": True,
            ".type": "device",
            ".name": "cfg030f15",
            "name": "br-lan",
            "type": "bridge",
            "ports": ["lan1", "lan2", "lan3"],
        },
        "cfg040f15": {
            ".anonymous": True,
            ".type": "device",
            ".name": "cfg040f15",
            "name": "lan3",
            "macaddr": "94:83:c4:00:00:33",
        },
        "lan": {
            ".anonymous": False,
            ".type": "interface",
            ".name": "lan",
            "device": "br-lan",
            "proto": "static",
        },
    }
}


class FakeOpenWrt:
    """Answers like OpenWrt 23.05's /ubus (uhttpd-mod-ubus + rpcd), from the
    JSON files in fixtures/ (named <object>__<method>.json)."""

    def __init__(self):
        self.routes = {}
        for f in FIXTURES.glob("*.json"):
            obj, method = f.stem.split("__")
            self.routes[(obj, method)] = json.loads(f.read_text())
        self.users = {"omini": "secret"}
        self.sessions: set[str] = set()
        self.denied: set[str] = set()  # "object method" the ACL refuses
        self.logins = 0
        self.calls: list[tuple[str, str]] = []
        # /etc/config/network as rpcd's `uci get` answers it, the changes each
        # session staged, and a pending `uci apply` with rollback.
        self.network = copy.deepcopy(NETWORK)
        self.staged: dict[str, dict] = {}
        self.pending: tuple[str, dict] | None = None  # (session, the file before)
        self.other_pending = False  # someone applied from LuCI and did not confirm yet
        self.uci_write_denied = False  # the ACL's "uci" section has no write for network
        self.ignore_changes = False  # netifd does not act on the reload
        self.confirmed = 0
        self.writes: list[tuple[str, dict]] = []  # uci calls that change something
        self.running = self.disabled_in(self.network)  # ports netifd has disabled
        self.sections = 0

    @staticmethod
    def disabled_in(network: dict) -> set[str]:
        return {
            s["name"]
            for s in network["values"].values()
            if s[".type"] == "device" and "type" not in s and s.get("enabled") == "0"
        }

    def uci(self, sid: str, method: str, args: dict):
        """rpcd's uci object (the network config only): returns (status, data)."""
        if args.get("config", "network") != "network":
            return 6, None
        work = self.staged.get(sid, self.network)
        if method == "get":
            return 0, {"values": copy.deepcopy(work)["values"]}
        if method in ("set", "add", "delete", "revert") and self.uci_write_denied:
            return 6, None
        if method in ("set", "add", "delete"):
            self.writes.append((method, args))
            work = self.staged.setdefault(sid, copy.deepcopy(self.network))
            values = work["values"]
            if method == "add":
                self.sections += 1
                name = f"cfg{self.sections:02x}0f15"
                values[name] = {".anonymous": True, ".type": args["type"], ".name": name}
                values[name].update(args.get("values") or {})
                return 0, {"section": name}
            section = values.get(args["section"])
            if section is None:
                return 4, None
            if method == "set":
                section.update(args["values"])
            else:
                section.pop(args["option"], None)
            return 0, None
        if method == "revert":
            self.staged.pop(sid, None)
            return 0, None
        if method == "apply":
            if self.other_pending or (self.pending and args.get("rollback")):
                return 6, None
            if sid not in self.staged:
                return 5, None
            self.writes.append((method, args))
            before, self.network = self.network, self.staged.pop(sid)
            if args.get("rollback"):
                self.pending = (sid, before)
            self.reload()
            return 0, None
        if method == "confirm":
            if not self.pending:
                return 5, None
            if self.pending[0] != sid:
                return 6, None
            self.pending = None
            self.confirmed += 1
            return 0, None
        if method == "rollback":
            if not self.pending:
                return 5, None
            self.writes.append((method, args))
            self.network = self.pending[1]
            self.pending = None
            self.reload()
            return 0, None
        return 3, None

    def reload(self):
        if not self.ignore_changes:
            self.running = self.disabled_in(self.network)

    def device_status(self, args: dict):
        devices = {
            k: v
            for k, v in self.routes[("network.device", "status")].items()
            if k not in self.running
        }
        name = args.get("name")
        if name is None:
            return 0, devices
        if name in self.running:
            return 0, {"external": False, "present": False, "type": "Network device"}
        return (0, devices[name]) if name in devices else (4, None)

    def expire_sessions(self):
        self.sessions.clear()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url.path != "/ubus":
            return httpx.Response(404, text="Not Found")
        body = json.loads(request.content)
        assert body["jsonrpc"] == "2.0" and body["method"] == "call"
        sid, obj, method, args = body["params"]
        rid = body["id"]

        def denied():
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32002, "message": "Access denied"},
                },
            )

        def ok(data):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": [0, data]})

        if (obj, method) == ("session", "login"):
            if self.users.get(args.get("username")) != args.get("password"):
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": [6]})
            self.logins += 1
            new = f"{self.logins:032x}"
            self.sessions.add(new)
            return ok(
                {
                    "ubus_rpc_session": new,
                    "timeout": 300,
                    "expires": 300,
                    "acls": {},
                    "data": {"username": args["username"]},
                }
            )
        if sid not in self.sessions or f"{obj} {method}" in self.denied:
            return denied()
        self.calls.append((obj, method))
        if obj == "uci" or (obj, method) == ("network.device", "status"):
            status, data = self.uci(sid, method, args) if obj == "uci" else self.device_status(args)
            if status:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": [status]})
            return ok(data if data is not None else {})
        if (obj, method) not in self.routes:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32000, "message": "Object not found"},
                },
            )
        data = self.routes[(obj, method)]
        if (obj, method) in KEYED:
            data = data.get(args.get(KEYED[(obj, method)]))
            if data is None:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": [4]})
        if (obj, method) == ("file", "read"):
            data = {"data": data}  # rpcd answers {"data": "<file contents>"}
        return ok(data)


@pytest.fixture
def openwrt(monkeypatch):
    fake = FakeOpenWrt()
    monkeypatch.setattr(
        collect_module, "Client", partial(Client, transport=httpx.MockTransport(fake.handler))
    )
    return fake


@pytest.fixture
def cfg(tmp_path):
    from omini_sdk import Config

    return Config(
        {"url": "https://192.168.1.1", "username": "omini", "password": "secret"},
        state_dir=tmp_path / "state",
    )
