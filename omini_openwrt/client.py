"""ubus JSON-RPC client (uhttpd-mod-ubus, the API LuCI uses), on the SDK's
HTTP client: timeouts, retries of a refused connection or a busy router, and
errors that are the SDK's (``AccessDenied`` is a ``Forbidden``, ``Unavailable``
a ``NotFound``), so ``omini_sdk.optional`` treats them like any plugin's.

Every request is a JSON-RPC 2.0 POST to <url>/ubus (POST is simply how
JSON-RPC carries the request). collect and test call only ubus methods that
read; only apply calls uci methods that change the network configuration.
The session id is kept in the state folder (the SDK's ``Session``), and a
refused password waits 10 minutes before the next login (``LoginBackoff``).
https://openwrt.org/docs/techref/ubus (section "Access to ubus over HTTP")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from omini_sdk import PluginError
from omini_sdk.http import Client as HttpClient
from omini_sdk.http import Forbidden, HttpError, LoginBackoff, NotFound, Session

__all__ = ["AccessDenied", "Client", "NotPermitted", "UbusError", "Unavailable"]

# The session id ubus accepts before login.
ANONYMOUS = "0" * 32

# JSON-RPC error codes of uhttpd-mod-ubus.
ACCESS_DENIED = -32002
OBJECT_NOT_FOUND = -32000
METHOD_NOT_FOUND = -32601

# ubus status codes (first element of "result").
STATUS_PERMISSION_DENIED = 6

REJECTED = "OpenWrt rejected the username or password"


class UbusError(HttpError):
    """A call ubus did not answer with data (the method is optional)."""


class AccessDenied(UbusError, Forbidden):
    """The session's ACL does not allow this method (or the session expired)."""


class NotPermitted(AccessDenied):
    """A live session whose ACL does not allow this (rpcd's permission denied
    status): logging in again would not change it."""


class Unavailable(UbusError, NotFound):
    """The object or method does not exist, or it answered an error status
    (not the SDK's Unavailable: the router did answer)."""


class Client(HttpClient):
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        verify_tls: bool = False,
        state_dir: Path | None = None,
        timeout: float = 15,
        transport: httpx.BaseTransport | None = None,
    ):
        super().__init__(
            url,
            name="OpenWrt",
            verify_tls=verify_tls,
            timeout=timeout,
            user_agent="omini-plugin-openwrt",
            unauthorized=REJECTED,
            transport=transport,
        )
        self.username = username
        self._password = password
        self.state_dir = state_dir
        self.saved = Session(state_dir, self.base, username, password)
        # After rpcd refuses the password, wait before signing in again:
        # Omini runs every minute.
        self.backoff_login = LoginBackoff(state_dir, self.base, username, password, minutes=10)
        self.session: str | None = None
        # True once the session in use was created by this run: an "Access
        # denied" then means the ACL, not an expired session.
        self.fresh = False
        self._id = 0

    # --- session ------------------------------------------------------------

    def _load_session(self) -> str | None:
        sid = self.saved.load().get("session")
        return sid if isinstance(sid, str) and sid else None

    def login(self) -> str:
        """session.login: returns a new session id (ubus_rpc_session)."""
        self.backoff_login.check()
        try:
            data = self._rpc(
                ANONYMOUS,
                "session",
                "login",
                {"username": self.username, "password": self._password},
            )
        except AccessDenied as e:
            # rpcd's answer to a wrong username or password.
            self.backoff_login.refused(REJECTED)
            raise PluginError(REJECTED) from e
        except Unavailable as e:
            raise PluginError(REJECTED) from e
        sid = data.get("ubus_rpc_session") if isinstance(data, dict) else None
        if not isinstance(sid, str) or not sid:
            raise PluginError(REJECTED)
        self.backoff_login.clear()
        self.session, self.fresh = sid, True
        self.saved.save({"session": sid})
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
        except AccessDenied as e:
            if self.fresh or isinstance(e, NotPermitted):
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
        r = self.send("POST", "/ubus", json=body)
        if r.status_code == 404:
            raise PluginError(
                f"{self.base} has no /ubus endpoint: is it OpenWrt 21.02 or later with "
                "LuCI (uhttpd-mod-ubus)?"
            )
        # uhttpd answers 403 and 500 with a JSON-RPC error, read below.
        if r.status_code not in (403, 500):
            self.check(r, "POST", "/ubus")
        answer = self.json(r, "/ubus")
        if not isinstance(answer, dict):
            raise PluginError(f"{self.base} did not answer like ubus")
        what = f"{obj} {method}"
        error = answer.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            if code == ACCESS_DENIED:
                raise AccessDenied(what, resource=what, method="POST")
            raise Unavailable(f"{what}: {error.get('message')}", resource=what, method="POST")
        result = answer.get("result")
        if not isinstance(result, list) or not result:
            raise PluginError(f"{self.base} did not answer like ubus")
        if result[0] == STATUS_PERMISSION_DENIED:
            raise NotPermitted(what, resource=what, method="POST")
        if result[0] != 0:
            raise Unavailable(f"{what}: status {result[0]}", resource=what, method="POST")
        return result[1] if len(result) > 1 else {}
