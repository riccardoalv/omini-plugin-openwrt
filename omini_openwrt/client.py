"""Minimal ubus JSON-RPC client (uhttpd-mod-ubus, the API LuCI uses).

Every request is a JSON-RPC 2.0 POST to <url>/ubus (POST is simply how
JSON-RPC carries the request). collect and test call only ubus methods that
read; only apply calls uci methods that change the network configuration.
https://openwrt.org/docs/techref/ubus (section "Access to ubus over HTTP")
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from omini_sdk import PluginError, log

# The session id ubus accepts before login.
ANONYMOUS = "0" * 32

# JSON-RPC error codes of uhttpd-mod-ubus.
ACCESS_DENIED = -32002
OBJECT_NOT_FOUND = -32000
METHOD_NOT_FOUND = -32601

# ubus status codes (first element of "result").
STATUS_PERMISSION_DENIED = 6


class UbusError(Exception):
    """A call ubus did not answer with data (the method is optional)."""


class AccessDenied(UbusError):
    """The session's ACL does not allow this method (or the session expired)."""


class Unavailable(UbusError):
    """The object or method does not exist, or it answered an error status."""


class Client:
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        verify_tls: bool = False,
        state_dir: Path | None = None,
        timeout: float = 10,
        transport: httpx.BaseTransport | None = None,
    ):
        url = url.strip().rstrip("/")
        if not url.startswith(("https://", "http://")):
            url = "https://" + url
        self.base = url
        self.username = username
        self._password = password
        self.state_dir = state_dir
        self.session: str | None = None
        # True once the session in use was created by this run: an "Access
        # denied" then means the ACL, not an expired session.
        self.fresh = False
        self._id = 0
        self.http = httpx.Client(
            base_url=url,
            verify=verify_tls,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "omini-plugin-openwrt"},
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()

    # --- session ------------------------------------------------------------

    @property
    def _session_file(self) -> Path | None:
        return self.state_dir / "session.json" if self.state_dir else None

    def _load_session(self) -> str | None:
        f = self._session_file
        if not f or not f.is_file():
            return None
        try:
            saved = json.loads(f.read_text())
        except (OSError, ValueError):
            return None
        if saved.get("url") != self.base or saved.get("username") != self.username:
            return None
        sid = saved.get("session")
        return sid if isinstance(sid, str) and sid else None

    def _save_session(self, sid: str) -> None:
        f = self._session_file
        if not f:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"url": self.base, "username": self.username, "session": sid}))
            os.chmod(f, 0o600)
        except OSError as e:
            log.debug("cannot save the session: %s", e)

    def login(self) -> str:
        """session.login: returns a new session id (ubus_rpc_session)."""
        try:
            data = self._rpc(
                ANONYMOUS,
                "session",
                "login",
                {"username": self.username, "password": self._password},
            )
        except (AccessDenied, Unavailable) as e:
            raise PluginError("OpenWrt rejected the username or password") from e
        sid = data.get("ubus_rpc_session") if isinstance(data, dict) else None
        if not isinstance(sid, str) or not sid:
            raise PluginError("OpenWrt rejected the username or password")
        self.session, self.fresh = sid, True
        self._save_session(sid)
        return sid

    def call(self, obj: str, method: str, args: dict[str, Any] | None = None) -> Any:
        """Calls a ubus method with the session, logging in when there is
        none. A saved session is reused until ubus refuses it (sessions expire
        after 5 minutes without use); it is then renewed once."""
        if self.session is None:
            saved = self._load_session()
            if saved:
                self.session, self.fresh = saved, False
            else:
                self.login()
        assert self.session is not None
        try:
            return self._rpc(self.session, obj, method, args or {})
        except AccessDenied:
            if self.fresh:
                raise
            self.login()
            return self._rpc(self.session, obj, method, args or {})

    # --- transport ----------------------------------------------------------

    def _rpc(self, sid: str, obj: str, method: str, args: dict[str, Any]) -> Any:
        self._id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": "call",
            "params": [sid, obj, method, args],
        }
        try:
            r = self.http.post("/ubus", json=body)
        except httpx.ConnectError as e:
            raise PluginError(f"cannot connect to {self.base}: {e}") from e
        except httpx.TimeoutException as e:
            raise PluginError(f"{self.base} did not answer in time") from e
        except httpx.HTTPError as e:
            raise PluginError(f"request to {self.base} failed: {e}") from e
        if r.status_code == 404:
            raise PluginError(
                f"{self.base} has no /ubus endpoint: is it OpenWrt 21.02 or later with "
                "LuCI (uhttpd-mod-ubus)?"
            )
        if r.status_code >= 400 and r.status_code not in (403, 500):
            raise PluginError(f"{self.base} answered {r.status_code}")
        try:
            answer = r.json()
        except json.JSONDecodeError as e:
            raise PluginError(
                f"{self.base} did not answer with JSON: is it the OpenWrt address?"
            ) from e
        if not isinstance(answer, dict):
            raise PluginError(f"{self.base} did not answer like ubus")
        error = answer.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            if code == ACCESS_DENIED:
                raise AccessDenied(f"{obj} {method}")
            raise Unavailable(f"{obj} {method}: {error.get('message')}")
        result = answer.get("result")
        if not isinstance(result, list) or not result:
            raise PluginError(f"{self.base} did not answer like ubus")
        if result[0] == STATUS_PERMISSION_DENIED:
            raise AccessDenied(f"{obj} {method}")
        if result[0] != 0:
            raise Unavailable(f"{obj} {method}: status {result[0]}")
        return result[1] if len(result) > 1 else {}
