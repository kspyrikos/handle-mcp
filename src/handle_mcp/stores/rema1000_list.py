"""REMA 1000: the cart is the shopping list called "handle-mcp" (docs/adr/0007).

The only list this reads or writes. It never touches another list, deletes a list, makes a
list primary, shares one, reads a list with its users, or calls REMA's own cart or payment.
The human opens the list in REMA's app, turns it into an order, picks the time and pays.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from typing import Any

from handle_mcp.models import Cart, CartLine, QuantityResult
from handle_mcp.stores import StoreError
from handle_mcp.stores.rema1000_auth import Unsure
from handle_mcp.stores.rema1000_pickup import GateClosed

LIST = "handle-mcp"
ATTEMPTS = 3  # a POST is sent again only after a read shows the first one did not land
WAIT = 1.0  # seconds between those reads
LIST_PAGES = 10
IS_LIST = (
    f'The cart is the list "{LIST}" in the REMA 1000 app. You place the order, choose a time and pay yourself '
    "at REMA 1000. The packing fee for Køb & Hent is unknown and is not included."
)
PRIMARY = f'REMA 1000 made the list "{LIST}" the primary list. Set your own list as primary again in the app.'


def _num(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _id(value: Any) -> int | None:
    """An id as REMA sends it: a number or a numeric string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) or (isinstance(value, str) and value.isascii() and value.isdigit()):
        return int(value)
    return None


def _qty(item: dict) -> int:
    n = _num(item.get("amount"))
    return int(n + Decimal("0.5")) if n is not None and n > 0 else 0


def _product(item: dict) -> str | None:
    """The product id of a line, None for a line written as free text."""
    pid = _id(item.get("store_item_id"))
    return str(pid) if pid and isinstance(item.get("store_item"), dict) else None


def line_total(item: dict) -> tuple[Decimal, Decimal]:
    """(price, deposit) of a line, as REMA's own list adds it up: past an offer's limit, the
    price over the limit (the plain price when REMA gives none)."""
    pricing = (item.get("store_item") or {}).get("pricing") or {}
    price = _num(pricing.get("price")) or Decimal(0)
    n = _qty(item)
    total = price * n
    most = _qty({"amount": pricing.get("max_quantity")})
    if pricing.get("is_on_discount") is True and 0 < most < n:
        over = _num(pricing.get("price_over_max"))
        total = price * most + (over if over is not None else price) * (n - most)
    return total, (_num(pricing.get("deposit")) or Decimal(0)) * n


class RemaListMixin:
    """get_cart and set_quantities on the list. Needs `_api()`, `_api_json()`, `_bearer()`,
    `_user` from RemaAuthMixin and `_pickup()` from RemaPickupMixin."""

    name: str
    _user: int | None
    _write_lock: asyncio.Lock

    async def _lists(self) -> tuple[int | None, int]:
        """The list's id (the lowest, when a lagging read let a second one through) and how
        many other lists have the name. Only the exact name counts."""
        await self._bearer()
        if not self._user:
            raise StoreError("REMA 1000 login returned no user ID. Its API may have changed.")
        ids: list[int] = []
        for page in range(1, LIST_PAGES + 1):
            data = await self._api_json(
                f"v3/users/{self._user}/shopping-lists",
                "your lists",
                {"per_page": 100, "page": page},
            )
            rows = data.get("data") if isinstance(data.get("data"), list) else []
            ids += [
                i
                for r in rows
                if isinstance(r, dict) and r.get("name") == LIST and (i := _id(r.get("id")))
            ]
            last = _id(((data.get("meta") or {}).get("pagination") or {}).get("last_page")) or 1
            if page >= last or not rows:
                break
        return (min(ids), len(ids) - 1) if ids else (None, 0)

    async def _read(self, list_id: int) -> dict:
        """One list with its lines only. Never with the users: that brings names and emails."""
        data = await self._api_json(f"v2/shoppinglists/{list_id}", "listen", {"with": "items"})
        data = data["data"] if isinstance(data.get("data"), dict) else data
        if _id(data.get("id")) != list_id or not isinstance(data.get("items") or [], list):
            raise StoreError(
                "REMA 1000 returned a different list than requested. Its API may have changed."
            )
        data["items"] = [i for i in data.get("items") or [] if isinstance(i, dict)]
        return data

    async def get_cart(self) -> Cart:
        try:
            pickup = (await self._pickup()).name
        except GateClosed:  # the list is also for shopping in the store yourself
            pickup = None
        cart = Cart(self.name, [], Decimal(0), {}, Decimal(0), delivery_mode="pickup",
                    pickup_store=pickup)  # fmt: skip
        list_id, extra = await self._lists()
        if list_id is None:
            cart.warnings.append(
                f'The list "{LIST}" does not exist yet. It is created when you add something to the cart.'
            )
            return cart
        full = await self._read(list_id)
        lines: dict[str, CartLine] = {}
        deposit, texts = Decimal(0), 0
        for item in full["items"]:
            if item.get("bought") is True:  # ticked off in the app: bought
                continue
            pid = _product(item)
            if pid is None:
                texts += 1
                continue
            total, pant = line_total(item)
            deposit += pant
            name = str(item["store_item"].get("name") or "")
            line = lines.setdefault(pid, CartLine(pid, name, 0, Decimal(0)))
            line.quantity += _qty(item)  # a product on two lines: their sum (docs/adr/0007)
            line.line_total += total
        cart.lines = list(lines.values())
        cart.products_total = sum((li.line_total for li in cart.lines), Decimal(0))
        if deposit:
            cart.fees["pant"] = deposit
        cart.total = cart.products_total + deposit
        cart.warnings.append(IS_LIST)
        if full.get("primary") is True:
            cart.warnings.append(PRIMARY)
        if extra:
            cart.warnings.append(
                f'There are {extra} more list(s) named "{LIST}". They are not used.'
            )
        if texts:
            cart.warnings.append(
                f"{texts} line(s) on the list are text without prices and are not included in the total."
            )
        return cart

    async def set_quantities(self, items: dict[str, int]) -> list[QuantityResult]:
        """Absolute amounts on the list; 0 removes. Read back and compared. Never orders or pays."""
        # Only the id as REMA writes it ("7", never "007"), so it matches the lines read back.
        want = {pid: n for pid, n in items.items() if str(_id(pid) or "") == pid and n >= 0}
        failed = {pid: "Invalid product ID or quantity." for pid in items if pid not in want}
        applied: dict[str, int] = {}
        warn = None
        if want:
            async with self._write_lock:  # one write at a time: two never both create the list
                applied, warn = await self._apply(want, failed)
        out = []
        for pid, n in items.items():
            if pid in failed:
                out.append(QuantityResult(pid, n, None, False, failed[pid]))
                continue
            got = applied.get(pid, 0)
            ok = got == n
            msg = None if ok else f"The list has {got} instead of {n}."
            out.append(QuantityResult(pid, n, got, ok, msg if not ok else warn))
        return out

    async def _apply(self, want: dict[str, int], failed: dict[str, str]) -> tuple[dict, str | None]:
        list_id, _ = await self._lists()
        if list_id is None:
            if not any(want.values()):
                return {}, None  # nothing to remove from a list that is not there
            list_id = await self._create()
        full = await self._read(list_id)
        base = f"v2/shoppinglists/{list_id}"
        lines: dict[str, list[dict]] = {}
        for item in full["items"]:
            if pid := _product(item):
                lines.setdefault(pid, []).append(item)
        # Ticked off in the app means bought: the next write removes it (docs/adr/0007), except
        # the one line _reconcile brings back for a product asked for again.
        reused = set()
        for pid, n in want.items():
            mine = lines.get(pid, [])
            if n and mine and all(i.get("bought") is True for i in mine):
                reused.add(id(mine[0]))
        gone = [i["id"] for i in full["items"] if i.get("bought") is True and id(i) not in reused]
        for pid, n in want.items():
            gone += await self._reconcile(base, pid, n, lines.get(pid, []), failed)
        cleanup = None
        if gone:
            body = {"items": [{"id": i, "delete": True} for i in dict.fromkeys(gone)]}
            try:
                await self._write("PATCH", base, body, None, failed)
            except StoreError:  # the changes above have landed: still read back and report
                cleanup = "REMA 1000 did not remove the old lines. Check the list in the app."
        back = await self._read(list_id)
        applied: dict[str, int] = {}
        for item in back["items"]:
            if (pid := _product(item)) and item.get("bought") is not True:
                applied[pid] = applied.get(pid, 0) + _qty(item)
        return applied, PRIMARY if back.get("primary") is True else cleanup

    async def _reconcile(
        self, base: str, pid: str, n: int, lines: list[dict], failed: dict[str, str]
    ) -> list[int]:
        """Bring one product to n. The first open line is set; extra lines are returned for
        removal. A product with only bought lines gets the first one back."""
        active = [i for i in lines if i.get("bought") is not True]
        bought = [i for i in lines if i.get("bought") is True]
        if n == 0:
            return [i["id"] for i in active]
        if active:
            if _qty(active[0]) != n:
                await self._write(
                    "PATCH", f"{base}/items/{active[0]['id']}", {"amount": n}, pid, failed
                )
            return [i["id"] for i in active[1:]]
        if bought:
            body = {"amount": n, "bought": False}
            await self._write("PATCH", f"{base}/items/{bought[0]['id']}", body, pid, failed)
            return []
        found = await self._add(base, pid, n, failed)
        return await self._reconcile(base, pid, n, found, failed) if found else []

    async def _add(self, base: str, pid: str, n: int, failed: dict[str, str]) -> list[dict]:
        """POST a line, never retried blindly. After a lost answer, read the list and POST
        again only if the product is still missing. Returns its lines when an earlier POST
        turned out to have landed."""
        body = {"amount": n, "store_id": 1, "store_item_id": int(pid)}  # 1 is fixed in REMA's SDK
        for attempt in range(ATTEMPTS):
            try:
                await self._write("POST", f"{base}/items", body, pid, failed, once=True)
                return []
            except Unsure:
                await asyncio.sleep(WAIT)
                list_id = int(base.rsplit("/", 1)[1])
                found = [i for i in (await self._read(list_id))["items"] if _product(i) == pid]
                if found:
                    return found
        # Noted per product, so the changes already made in this call are still reported.
        failed[pid] = "REMA 1000 is not responding, and the product was not added to the list. Try again shortly."
        return []

    async def _create(self) -> int:
        """Make the list, never primary. After a lost answer, look again before a new POST."""
        body = {"name": LIST, "primary": False, "items": []}
        for _ in range(ATTEMPTS):
            unsure = False
            try:
                await self._write("POST", "v2/shoppinglists", body, None, {}, once=True)
            except Unsure:
                unsure = True
                await asyncio.sleep(WAIT)
            list_id, _ = await self._lists()
            if list_id is not None:
                return list_id
            if not unsure:
                raise StoreError(f'REMA 1000: the list "{LIST}" was not found after creation.')
        raise StoreError(f'REMA 1000 is not responding, and the list "{LIST}" was not created. Try again.')

    async def _write(
        self,
        method: str,
        path: str,
        body: dict,
        pid: str | None,
        failed: dict[str, str],
        once: bool = False,
    ) -> None:
        """One change. A 4xx on a product is REMA refusing it: noted, the rest goes on."""
        resp = await self._api(method, path, body=body, once=once)
        status = resp.status_code
        if 200 <= status < 300:
            return
        if 400 <= status < 500 and pid:
            failed[pid] = f"REMA 1000 rejected the product ({status}). Is it in the assortment?"
            return
        raise StoreError(f"REMA 1000 rejected the list update ({status}).")
