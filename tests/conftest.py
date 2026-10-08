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
