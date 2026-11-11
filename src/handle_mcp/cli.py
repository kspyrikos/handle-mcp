"""Command line for a human in a terminal: save or remove a store login.

    handle-mcp login nemlig             asks for email and password, logs in, saves the store's
                                        session in the keychain
    handle-mcp login nemlig --from-env  reads HANDLE_NEMLIG_EMAIL/_PASSWORD, logs in, saves the
                                        session in ~/.config/handle-mcp/nemlig.json (chmod 600).
                                        For a headless machine or an AI agent with no terminal.
    handle-mcp logout nemlig            removes the login from the keychain and the file

A store that needs a pickup store (REMA 1000) asks for it after the login, on this terminal.
With --from-env, set HANDLE_<STORE>_STORE_ID (store ids, comma-separated) for the server.

The password is used for one login and then forgotten: never saved, echoed or logged. Only the
session the store gives back is saved (docs/adr/0003). When it expires, log in again.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import sys
from typing import Any

from keyring.errors import KeyringError

from handle_mcp import stores as registry
from handle_mcp import telemetry, translation


def configure_logging() -> None:
    """Log to stderr. httpx logs every request URL at INFO, and URLs can hold ids and
    tokens. MCP clients keep stderr in log files, so httpx only speaks on warnings."""
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(mcp: Any) -> None:
    """`handle-mcp`: the MCP server over stdio, or with arguments this command line."""
    configure_logging()
    args = sys.argv[1:]
    if "--no-telemetry" in args:
        telemetry.disable()
        args.remove("--no-telemetry")
    if args:  # `handle-mcp login <store>` from a terminal, not from an MCP client
        sys.exit(run(args))
    telemetry.start()
    mcp.run("stdio")


def _say(text: str) -> None:
    sys.stderr.write(translation.translate_text(text) + "\n")


def run(args: list[str]) -> int:
    known = ", ".join(sorted(registry.STORES))
    from_env = "--from-env" in args
    args = [a for a in args if a != "--from-env"]
    if len(args) != 2 or args[0] not in ("login", "logout") or args[1] not in registry.STORES:
        _say(
            "Usage: handle-mcp login <store> [--from-env] or handle-mcp logout <store>\n"
            f"Stores: {known}"
        )
        return 2
    command, store = args
    if command == "logout":
        return _logout(store)
    if from_env:
        return _login_from_env(store)

    _say(f"Log in to {store} using the email address and password you use on the store website.")
    sys.stderr.write(translation.translate_text(f"Email for {store}: "))
    sys.stderr.flush()
    email = sys.stdin.readline().strip()
    password = getpass.getpass(translation.translate_text(f"Password for {store} (hidden): "))
    if not email or not password:
        _say("Both email and password are required. Nothing was saved.")
        return 1
    try:  # log in first, save after: a failed login never touches a saved one
        session = _check(store, email, password, choose=True)
    except registry.StoreError as exc:
        _say(f"Login was not saved: {exc}\nAny previously saved login remains unchanged.")
        return 1
    try:
        registry.keychain_set(store, email, session)
    except KeyringError as exc:
        _say(f"Could not save to the keychain: {exc}")
        return 1
    _say(f"Logged in to {store}. The session was saved in the keychain. The password was not saved.")
    return 0


def _logout(store: str) -> int:
    removed = ["config file"] if registry.config_delete(store) else []
    try:
        if registry.keychain_delete(store):
            removed.append("keychain")
    except KeyringError as exc:
        if not removed:
            _say(f"No keychain is available on this machine: {exc}")
            return 1
    _say(f"Login removed from {' and '.join(removed)}." if removed else "There was no saved login.")
    return 0


def _login_from_env(store: str) -> int:
    """No terminal, no keychain: a headless box or an AI agent. The env vars are read once,
    checked against the store and saved in a file only the owner can read."""
    prefix = f"HANDLE_{store.upper()}_"
    email, password = os.environ.get(prefix + "EMAIL"), os.environ.get(prefix + "PASSWORD")
    if not email or not password:
        _say(f"Set {prefix}EMAIL and {prefix}PASSWORD, then try again. Nothing was saved.")
        return 1
    try:  # log in first, save after: a failed login never touches a saved one
        session = _check(store, email, password)
    except registry.StoreError as exc:
        _say(f"Login was not saved: {exc}\nAny previously saved login remains unchanged.")
        return 1
    path = registry.config_set(store, email, session)
    _say(
        f"Logged in to {store}. The session was saved to {path} (readable only by you). "
        f"The password was not saved; remove {prefix}PASSWORD from your environment."
    )
    try:
        shadowed = registry.keychain_get(store) is not None
    except registry.StoreError:  # a broken entry: load_login will complain about it too
        shadowed = True
    if shadowed:  # load_login reads the keychain first
        _say(
            f"WARNING: the keychain also has a login for {store}, and it takes precedence over the file. "
            f"Remove it with: handle-mcp logout {store}, then run --from-env again."
        )
    return 0


def _ask(text: str) -> str:
    sys.stderr.write(translation.translate_text(text))
    sys.stderr.flush()
    return sys.stdin.readline().strip()


def _numbers(raw: str, most: int) -> list[int]:
    """ "2, 1" as list positions 1..most, in order, without repeats."""
    out: list[int] = []
    for part in raw.replace(" ", ",").split(","):
        if part.isdigit() and 1 <= int(part) <= most and int(part) not in out:
            out.append(int(part))
    return out


async def _choose(instance: Any) -> None:
    """Pick the stores in order of preference. The list is the store's own, shown here only."""
    _say("Choose the stores you want to use, in your preferred order.")
    while query := _ask("Search for a store (postcode, city or name; leave blank to skip): "):
        choices = await instance.store_choices(query)
        if not choices:
            _say("No stores matched. Try a different search.")
            continue
        for i, (_, text) in enumerate(choices, 1):
            _say(f"{i:>3}. {text}")
        picked = _numbers(_ask("Enter the numbers, e.g. 2,1 (leave blank to search again): "), len(choices))
        if picked:
            instance.choose_stores([choices[i - 1][0] for i in picked])
            _say("Selected: " + " | ".join(choices[i - 1][1] for i in picked))
            return
    _say(f"No store selected. Choose one later with: handle-mcp login {instance.name}")


async def _verify(instance: Any, choose: bool) -> None:
    check = getattr(instance, "check_login", None)  # a store with no cart to read yet
    await (check() if check else instance.get_cart())
    if choose and hasattr(instance, "store_choices"):
        await _choose(instance)


def _check(store: str, email: str, password: str, choose: bool = False) -> dict[str, str]:
    """A real, read-only login with these values (they win over saved ones via env vars).
    With `choose`, a store that needs a pickup store asks for it. Returns the session."""
    prefix = f"HANDLE_{store.upper()}_"
    keys = {prefix + "EMAIL": email, prefix + "PASSWORD": password}
    before = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    instance = registry.STORES[store]()
    try:
        asyncio.run(_verify(instance, choose))
    finally:
        for k, v in before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    session = instance.session()
    if not session:
        raise registry.StoreError(f"{store} did not return a session. Its API may have changed.")
    return session
