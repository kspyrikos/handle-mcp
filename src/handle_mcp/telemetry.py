"""Anonymous usage counts: how many carts and kroner handle-mcp fills in each store per week.

No id, no names, no addresses, no logins, no products, no order ids (docs/adr/0005). The only
file is `telemetry.json` in the config folder: the notice was shown, the last day each
daily event went out, and which stores have a counted cart that is not empty yet.
The funnel: server_started, first_tool_call, login_ok, cart_filled (list_filled for REMA 1000).
Off with HANDLE_TELEMETRY=0, DO_NOT_TRACK=1 or `--no-telemetry`.
Every send runs in a background thread with a short timeout. A failure is logged, never raised.
"""

from __future__ import annotations

import atexit
import functools
import json
import logging
import os
import platform
import sys
import threading
from threading import Thread
from collections.abc import Callable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx

from handle_mcp import stores as registry

log = logging.getLogger(__name__)

URL = "https://cloud.umami.is/api/send"  # Umami's event API; HANDLE_TELEMETRY_URL overrides
WEBSITE = "a6431afb-4982-436d-8b11-bfb33da3baa9"  # handlemcp.dk in Umami; the hostname splits app from site
HOSTNAME = "app.handlemcp.dk"  # a label in Umami, not a server
TIMEOUT = 2.0  # seconds
QUIET = 1800.0  # seconds without a cart change before the cart counts as filled
ROUND_TO = Decimal(50)  # kroner
STORE_NAMES = {"bilkatogo": "bilka"}
FILLED = {"rema1000": "list_filled"}  # REMA's cart is a shopping list: never mixed with carts
OFF = {"0", "false", "no", "off"}

NOTICE = """\
handle-mcp anonymously collects usage statistics: application starts, first tool 
usage and daily logins, and filled shopping carts (store, amount rounded to 50 DKK, 
number of items). It includes the application version, operating system and date. 
No names, addresses, login credentials, products, order IDs or user/device 
identifiers are collected.
Disable this using: HANDLE_TELEMETRY=0 eller: handle-mcp --no-telemetry
Læs mere: https://github.com/NJohansen/handle-mcp#brugsstatistik
"""

_off = False
_noticed = False  # notice() asked the file once in this process
_sent_today: dict[str, str] = {}  # event key -> day, so the file is read once per day
_lock = threading.Lock()  # _pending
_file_lock = threading.Lock()  # telemetry.json: the timer thread and the event loop both write
_pending: dict[str, tuple[threading.Timer, dict[str, Any]]] = {}


def _safe(fn: Callable[..., None]) -> Callable[..., None]:
    """Telemetry never breaks a tool: every error ends here, in the log."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            log.debug("telemetry failed", exc_info=True)

    return wrapper


def disable() -> None:
    global _off
    _off = True


def enabled() -> bool:
    if _off or os.environ.get("HANDLE_TELEMETRY", "").strip().lower() in OFF:
        return False
    return os.environ.get("DO_NOT_TRACK", "").strip().lower() in {"", *OFF}


def _version() -> str:
    try:
        return version("handle-mcp")
    except PackageNotFoundError:
        return "unknown"


def _state_file():
    return registry.CONFIG_DIR / "telemetry.json"


def _state() -> dict[str, Any]:
    try:
        state = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _update(change: Callable[[dict[str, Any]], dict[str, Any] | None]) -> None:
    """Read, change and write the file in one step. `change` returns None to leave it as is."""
    with _file_lock:
        if (new := change(_state())) is not None:
            _save(new)


def _save(state: dict[str, Any]) -> None:
    path = _state_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # the login folder: owner only
    path.write_text(json.dumps(state), encoding="utf-8")


@_safe
def start() -> None:
    """At server start: the notice once per machine, then today's "server_started"."""
    if not enabled():
        return
    if not _state().get("notice_shown"):
        sys.stderr.write(NOTICE)  # the log; notice() puts it where the human sees it
    _daily("server_started", {"os": platform.system().lower() or "unknown"})


def notice() -> str | None:
    """The notice for the first tool answer on this machine, once. The model tells the human:
    stderr ends in a log file nobody reads, and counting must never be a secret."""
    global _noticed
    if _noticed or not enabled():
        return None
    _noticed = True
    try:
        with _file_lock:
            state = _state()
            if state.get("notice_shown"):
                return None
            _save({**state, "notice_shown": True})
    except Exception:
        log.debug("telemetry notice failed", exc_info=True)
        return None
    return " ".join(NOTICE.split())


def counted(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap an async tool: count the day's first call. Listing the tools is not a call."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        tool_called(fn.__name__)
        return await fn(*args, **kwargs)

    return wrapper


@_safe
def tool_called(tool: str) -> None:
    """The first tool call of the day. A catalog or scanner only lists the tools: never here."""
    _daily("first_tool_call", {"tool": tool})


@_safe
def login_ok(store: str) -> None:
    """A store answered with the user's own cart, so the login works. Once per store per day."""
    name = STORE_NAMES.get(store, store)
    _daily("login_ok", {"store": name}, key=f"login_ok:{name}")


def _daily(event: str, data: dict[str, Any], key: str | None = None) -> None:
    """Send `event` at most once per machine per day. The file keeps the day, never an id."""
    key = key or event
    today = date.today().isoformat()
    if _sent_today.get(key) == today or not enabled():
        return
    _sent_today[key] = today
    new = False

    def mark(state: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal new
        days = state.get("days") if isinstance(state.get("days"), dict) else {}
        if days.get(key) == today:
            return None
        new = True
        return {**state, "days": {**days, key: today}}

    _update(mark)
    if new:
        _send(event, data)


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def basket(cart: Any) -> tuple[int, int]:
    """(total in kroner rounded to the nearest 50, number of items) of a cart."""
    total = Decimal(str(_get(cart, "total") or 0))
    items = sum(int(_get(line, "quantity") or 0) for line in _get(cart, "lines") or [])
    return int((total / ROUND_TO).quantize(Decimal(1), ROUND_HALF_UP) * ROUND_TO), items


@_safe
def cart_filled(store: str, cart: Any, results: list[Any]) -> None:
    """A cart changed. Counted once per order, only when every line went in: the event waits
    for QUIET seconds without changes, then the store is locked until a read sees it empty."""
    if not enabled() or not all(_get(r, "ok") for r in results):
        return
    total, items = basket(cart)
    if not items:  # the human had the cart emptied: not a filled one, and not an order
        with _lock:
            if old := _pending.pop(store, None):
                old[0].cancel()
        _update(lambda state: _with_counted(state, _counted(state) - {store}))
        return
    if store in _counted(_state()):
        return
    with _lock:
        if old := _pending.pop(store, None):
            old[0].cancel()
        data: dict[str, Any] = {"store": STORE_NAMES.get(store, store)}
        data["basket_total_dkk"] = total
        data["item_count"] = items
        timer = threading.Timer(QUIET, _flush, args=(store,))
        timer.daemon = True
        _pending[store] = (timer, data)
        timer.start()


@_safe
def cart_seen(store: str, cart: Any) -> None:
    """Every cart read. An empty cart means the order went through (or the human cleared it on
    the store's site): a cart still waiting counts now, and the store is unlocked."""
    if not enabled() or basket(cart)[1]:
        return
    _flush(store)
    _update(lambda state: _with_counted(state, _counted(state) - {store}))


def locked(store: str) -> bool:
    """The store's cart was counted and no tool has seen it empty since. The server then reads
    the cart before a write, so a new order after checkout is not missed."""
    return enabled() and store in _counted(_state())


def _counted(state: dict[str, Any]) -> set[str]:
    """Stores whose cart was counted and has not been empty since. Store names, never an id."""
    value = state.get("counted")
    return {str(s) for s in value} if isinstance(value, list) else set()


def _with_counted(state: dict[str, Any], stores: set[str]) -> dict[str, Any] | None:
    return None if stores == _counted(state) else {**state, "counted": sorted(stores)}


@_safe
def _flush(store: str, wait: bool = False) -> None:
    with _lock:
        entry = _pending.pop(store, None)
    if entry:
        entry[0].cancel()
        _update(lambda state: _with_counted(state, _counted(state) | {store}))
        _send(FILLED.get(store, "cart_filled"), entry[1], wait)


@atexit.register
def _flush_all() -> None:
    """The MCP client closes the server: count the carts still waiting, then stop."""
    for store in list(_pending):
        _flush(store, wait=True)


def _send(event: str, data: dict[str, Any], wait: bool = False) -> None:
    website = os.environ.get("HANDLE_TELEMETRY_WEBSITE", WEBSITE)
    if not website:
        log.debug("telemetry: no website id, %s not sent", event)
        return
    data = {**data, "version": _version(), "date": date.today().isoformat()}
    payload = {
        "type": "event",
        "payload": {
            "website": website,
            "hostname": HOSTNAME,
            "url": "/".join(["", event, *([data["store"]] if "store" in data else [])]),
            "name": event,
            "data": data,
        },
    }
    thread = Thread(target=_post, args=(payload,), daemon=True)
    thread.start()
    if wait:  # at exit: give the last event its timeout before Python stops the thread
        thread.join(TIMEOUT)


def _post(payload: dict[str, Any]) -> None:
    try:
        httpx.post(
            os.environ.get("HANDLE_TELEMETRY_URL") or URL,
            json=payload,
            headers={"User-Agent": f"Mozilla/5.0 (compatible; handle-mcp/{_version()})"},
            timeout=TIMEOUT,
        )
    except Exception:
        log.debug("telemetry: send failed", exc_info=True)
