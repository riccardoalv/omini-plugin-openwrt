"""The router's configuration as `uci export` prints it, for Omini's backups.

Only configuration (the uci files), never status, leases or counters, so two
backups differ only when the configuration changed. Secrets (Wi-Fi keys,
PPPoE and other passwords, WireGuard keys) are left out."""

from __future__ import annotations

from typing import Any

# The configs a backup holds, in this order.
CONFIGS = ("network", "wireless", "dhcp", "firewall", "system")

# Options whose value is a secret: never part of a backup.
SECRETS = {
    "key",
    "key1",
    "key2",
    "key3",
    "key4",
    "password",
    "sae_password",
    "auth_secret",
    "acct_secret",
    "dae_secret",
    "private_key",
    "preshared_key",
    "psk",
}


def quote(value: Any) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def _order(values: dict[str, Any]) -> list[dict[str, Any]]:
    """Sections in the file's order (rpcd's `.index`), else as answered."""
    secs = [s for s in values.values() if isinstance(s, dict) and s.get(".type")]
    if all(isinstance(s.get(".index"), int) for s in secs):
        return sorted(secs, key=lambda s: s[".index"])
    return secs


def export(name: str, config: Any) -> str:
    """One config as `uci export <name>` prints it."""
    values = config.get("values") if isinstance(config, dict) else None
    lines = [f"package {name}", ""]
    for s in _order(values if isinstance(values, dict) else {}):
        head = f"config {s['.type']}"
        anonymous = s.get(".anonymous")
        if not isinstance(anonymous, bool):
            anonymous = str(s.get(".name") or "cfg").startswith("cfg")
        if not anonymous and s.get(".name"):
            head += f" {quote(s['.name'])}"
        lines.append(head)
        for opt, value in s.items():
            if opt.startswith(".") or opt.lower() in SECRETS:
                continue
            if isinstance(value, list):
                lines += [f"\tlist {opt} {quote(v)}" for v in value]
            else:
                lines.append(f"\toption {opt} {quote(value)}")
        lines.append("")
    return "\n".join(lines) + "\n"


def render(configs: dict[str, Any]) -> str:
    """Every readable config, in CONFIGS order."""
    return "".join(export(n, configs[n]) for n in CONFIGS if configs.get(n))
