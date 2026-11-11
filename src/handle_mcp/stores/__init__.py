"""The stores. A new store = one file here + one line in STORES.

Every store is a class with the async methods in `Store`. If a store cannot do
something, the method raises NotImplementedError and the tool says so politely.
Store methods may only *read*, except `set_quantities`. Ordering, paying,
choosing a delivery time and changing the address must never exist here.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Protocol

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from handle_mcp.models import (
    Cart,
    DeliveryInfo,
    DeliverySlot,
    Order,
    Product,
    ProductNutrition,
    QuantityResult,
    Recipe,
    RecipeDetails,
    StoreTerms,
)

log = logging.getLogger(__name__)
CONFIG_DIR = Path(os.environ.get("HANDLE_CONFIG_DIR", "~/.config/handle-mcp")).expanduser()
KEYCHAIN_SERVICE = "handle-mcp"  # one entry per store; the secret is {"email", "session"} as JSON


class StoreError(Exception):
    """A clear, user-facing failure from one store (login failed, API changed, ...)."""


class NotFound(StoreError):
    """The store does not know this product (HTTP 404)."""


class Store(Protocol):
    name: str
    cart_page: str  # the store's cart page, where the human pays: gets UTM tags (links.py)

    async def search(self, query: str, limit: int = 10) -> list[Product]: ...
    async def recipes(self, query: str, limit: int = 5) -> list[Recipe]: ...
    async def recipe(self, recipe_id: str, persons: int | None = None) -> RecipeDetails: ...
    async def featured_recipes(self, limit: int = 20) -> list[Recipe]: ...
    async def recipes_by_tag(
        self, tags: list[str], category: str | None = None, limit: int = 20
    ) -> list[Recipe]: ...
    async def offers(self, limit: int = 25) -> list[Product]: ...
    async def nutrition(self, product_ids: list[str]) -> list[ProductNutrition]: ...
    async def get_cart(self) -> Cart: ...
    async def set_quantities(self, items: dict[str, int]) -> list[QuantityResult]: ...
    async def delivery_info(self) -> DeliveryInfo: ...
    async def delivery_slots(self, days: int = 7) -> list[DeliverySlot]: ...
    async def terms(self) -> StoreTerms: ...
    async def orders(self, limit: int = 10) -> list[Order]: ...
    async def order(self, order_id: str) -> Order: ...
    def session(self) -> dict[str, str] | None: ...  # what to save after a login: never a password


def _parse(data: object) -> dict | None:
    """A saved login: {"email", "session"}, or the old {"email", "password"} (still read, and
    replaced by a session on the next login: docs/adr/0003)."""
    if not isinstance(data, dict) or not isinstance(data.get("email"), str):
        return None
    if isinstance(data.get("session"), dict) and data["session"]:
        return {"email": data["email"], "session": data["session"]}
    if isinstance(data.get("password"), str) and data["password"]:
        return {"email": data["email"], "password": data["password"]}
    return None


def keychain_get(store: str) -> dict | None:
    """The login saved by `handle-mcp login <store>` in the OS keychain, if any."""
    try:
        secret = keyring.get_password(KEYCHAIN_SERVICE, store)
    except KeyringError:  # no keychain on this machine (e.g. a headless Linux box)
        return None
    if not secret:
        return None
    try:
        login = _parse(json.loads(secret))
    except ValueError:
        login = None
    if login is None:
        raise StoreError(f"Invalid login in the keychain. Run: handle-mcp login {store}")
    return login


def keychain_set(store: str, email: str, session: dict[str, str]) -> None:
    """Save the store's session, never the password (docs/adr/0003)."""
    keyring.set_password(KEYCHAIN_SERVICE, store, json.dumps({"email": email, "session": session}))


def keychain_delete(store: str) -> bool:
    """True if a login was removed, False if none was saved. Raises KeyringError with no keychain."""
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, store)
    except PasswordDeleteError:
        return False
    return True


def config_set(store: str, email: str, session: dict[str, str]) -> Path:
    """Save a session in ~/.config/handle-mcp/<store>.json, readable by the owner only.
    Used by `handle-mcp login <store> --from-env` where there is no keychain."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(stat.S_IRWXU)
    path = CONFIG_DIR / f"{store}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, stat.S_IRUSR | stat.S_IWUSR)
    if hasattr(os, "fchmod"):  # an existing wider file: tighten before writing (not on Windows)
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
    os.ftruncate(fd, 0)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"email": email, "session": session}, f)
    return path


def config_delete(store: str) -> bool:
    """True if ~/.config/handle-mcp/<store>.json was removed."""
    try:
        (CONFIG_DIR / f"{store}.json").unlink()
    except FileNotFoundError:
        return False
    return True


def has_login(store: str) -> bool:
    """True when load_login(store) would succeed. For calls that work without one."""
    try:
        load_login(store)
    except StoreError:
        return False
    return True


def _env(store: str) -> dict | None:
    prefix = f"HANDLE_{store.upper()}_"
    email, password = os.environ.get(prefix + "EMAIL"), os.environ.get(prefix + "PASSWORD")
    return {"email": email, "password": password} if email and password else None


def load_login(store: str) -> dict:
    """A store's login: {"email", "session"} or {"email", "password"}. First found wins:

    1. Env vars HANDLE_<STORE>_EMAIL and HANDLE_<STORE>_PASSWORD.
    2. The OS keychain (macOS Keychain, Windows Credential Locker, Secret Service),
       saved with `handle-mcp login <store>`. Recommended: encrypted at rest.
    3. ~/.config/handle-mcp/<store>.json, readable by its owner only (chmod 600).
    """
    if login := _env(store):
        return login
    if saved := keychain_get(store):
        return saved

    path = CONFIG_DIR / f"{store}.json"
    if not path.exists():
        raise StoreError(
            f"No login saved for {store}. Run in a terminal: handle-mcp login {store}. "
            f"Without a terminal, set HANDLE_{store.upper()}_EMAIL and _PASSWORD, "
            f"then run: handle-mcp login {store} --from-env"
        )
    # Windows has no mode bits (always 0o666); the user profile's ACL keeps the file private.
    if os.name != "nt" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise StoreError(f"Other users can read {path}. Run: chmod 600 {path}")
    try:
        login = _parse(json.loads(path.read_text(encoding="utf-8")))
    except ValueError:
        login = None
    if login is None:
        raise StoreError(f"{path} is invalid. Run: handle-mcp login {store} --from-env")
    return login


def save_session(store: str, email: str, session: dict[str, str]) -> None:
    """After a login with a password: replace a saved password with the session, where
    load_login found it. Env vars are the human's own: nothing is written then."""
    if _env(store):
        return
    try:
        if keychain_get(store) is not None:
            keychain_set(store, email, session)
        elif (CONFIG_DIR / f"{store}.json").exists():
            config_set(store, email, session)
    except (KeyringError, OSError, StoreError) as exc:  # the login works; only the save failed
        log.warning("could not save the %s session: %s", store, type(exc).__name__)


def expired(store: str) -> StoreError:
    return StoreError(f"The login for {store} has expired. Run in a terminal: handle-mcp login {store}")


from handle_mcp.stores.bilkatogo import BilkaStore
from handle_mcp.stores.nemlig import NemligStore
from handle_mcp.stores.rema1000 import Rema1000Store

STORES: dict[str, type[Store]] = {
    "nemlig": NemligStore,
    "bilkatogo": BilkaStore,
    "rema1000": Rema1000Store,
}
