"""nemlig.com: the private web API the website uses. See docs/research.md.

Login: XSRF token → short Bearer token (5 min) → POST /webapi/login, which sets the
.ASPXAUTH cookie (1 year). The Bearer token is renewed on 401.
Never add order, checkout or payment endpoints here (docs/adr/0001).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from handle_mcp.models import (
    Cart,
    CartLine,
    Order,
    Product,
    QuantityResult,
    StoreTerms,
)
from handle_mcp.stores import NotFound, StoreError, expired, has_login, load_login, save_session
from handle_mcp.stores._http import request
from handle_mcp.stores.nemlig_delivery import DK, DeliveryMixin, _money, _time
from handle_mcp.stores.nemlig_nutrition import NutritionMixin
from handle_mcp.stores.nemlig_offers import OffersMixin
from handle_mcp.stores.nemlig_recipes import RecipesMixin

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO logs full URLs with tokens

BASE = "https://www.nemlig.com"
SEARCH = "https://webapi.prod.knl.nemlig.it/searchgateway/api/search"
APP_VERSION = os.environ.get("HANDLE_NEMLIG_APP_VERSION", "11.201.0")
SETTINGS_TTL = 600  # seconds to reuse timeslot/zone for search
STATUS = {1: "Bestilt", 2: "Under behandling", 4: "Leveret"}
UNITS = {"kg": "kg", "l": "l", "ltr": "l", "stk": "stk"}
FREE = "ProductCampaignFreeProduct"
# Bags for a normal week (~800 kr), seen live 2026-11. The basket only says the bag price when it
# has products, so compare_basket uses this for an empty cart.
BAG_FEE = Decimal("17.85")


def _bag_fee() -> Decimal:
    """HANDLE_NEMLIG_BAG_FEE overrides the estimate. A bad value logs and falls back."""
    raw = os.environ.get("HANDLE_NEMLIG_BAG_FEE")
    if not raw:
        return BAG_FEE
    try:
        return Decimal(raw.strip().replace(",", "."))
    except InvalidOperation:
        log.warning("HANDLE_NEMLIG_BAG_FEE=%r is not a number. Using %s DKK.", raw, BAG_FEE)
        return BAG_FEE


def _unit(label: str | None) -> str | None:
    """'kr./Kg.' → 'kg', 'kr/l' → 'l', 'kr/stk' → 'stk'."""
    if not label:
        return None
    tail = label.lower().replace(".", "").split("/")[-1].strip()
    return UNITS.get(tail, tail or None)


def _offer(campaign: dict | None) -> str | None:
    """Text for a real campaign. 'DiscountItem' alone is nemlig's everyday 'Discount' label,
    not an offer, so it gives no text (docs/research.md)."""
    if not campaign:
        return None
    qty, total = campaign.get("MinQuantity"), _money(campaign.get("TotalPrice"))
    if qty and qty > 1 and total:
        return f"{qty} for {_kr(total)} kr"
    if (pct := campaign.get("DiscountPercent")) and _discount_price(campaign):
        return f"-{pct} %"
    return "Offer" if campaign.get("Type") else None  # a campaign we cannot describe


def _discount_price(campaign: dict | None) -> Decimal | None:
    """Discount campaigns: the hit's Price is the price BEFORE the offer, CampaignPrice is what
    you pay. Multi-buy campaigns (MinQuantity > 1) keep the single price. A free gift
    (ProductCampaignFreeProduct) costs 0."""
    if not campaign or (campaign.get("MinQuantity") or 0) > 1:
        return None
    if campaign.get("Type") == FREE:
        return Decimal(0)
    return _money(campaign.get("CampaignPrice")) or None


LOGIN_FLAGS = {"CheckForExistingProducts": True, "DoMerge": True}
LOGIN_FLAGS |= {"AppInstalled": False, "SaveExistingBasket": False}
AUTH_COOKIE = ".ASPXAUTH"  # set by /webapi/login, lives 1 year


def _until(campaign: dict | None) -> date | None:
    """Last Danish day of the campaign. IntervalEnd is UTC, e.g. 22:59:59Z = 23:59:59 in DK."""
    end = _time((campaign or {}).get("IntervalEnd"))
    return end.astimezone(DK).date() if end and end.tzinfo else (end.date() if end else None)


def _kr(value: Decimal) -> str:
    return f"{value:.2f}".replace(".", ",").removesuffix(",00")


class NemligStore(RecipesMixin, OffersMixin, DeliveryMixin, NutritionMixin):
    name = "nemlig"
    cart_page = f"{BASE}/kurv"  # the page the human opens to see the cart and pay

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=20, follow_redirects=True)
        self._bearer: str | None = None
        self._xsrf: str | None = None
        self._logged_in = False
        self._resumed = False  # logged in with a saved session, not a password
        self._settings: tuple[float, dict[str, Any], bool] | None = None  # (at, values, logged)
        self._slot_id = ""  # the basket's delivery time, for productbff calls
        self._lock = asyncio.Lock()

    # --- HTTP -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Device-Size": "desktop",
            "Platform": "web",
            "Version": APP_VERSION,
            "X-Correlation-Id": str(uuid.uuid4()),
            "Referer": f"{BASE}/",
            "User-Agent": "Mozilla/5.0 (handle-mcp)",
        }
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        if self._xsrf:
            headers["X-XSRF-TOKEN"] = self._xsrf
        return headers

    async def _send(self, method: str, url: str, **kwargs: Any) -> Any:
        """One request with retries on timeout/429/5xx. Returns parsed JSON."""
        resp = await request(
            self._client, method, url, "nemlig.com", headers=self._headers(), **kwargs
        )
        host = resp.url.host
        if host == "queue-it.net" or host.endswith(".queue-it.net"):
            raise StoreError("nemlig.com currently has a queue (queue-it). Try again shortly.")
        if resp.status_code == 401:
            raise _Unauthorized
        if resp.status_code >= 400:  # the URL may hold ids: stderr only, path without query
            log.warning("nemlig.com %s %s -> %s", method, resp.url.path, resp.status_code)
            error = NotFound if resp.status_code == 404 else StoreError
            raise error(f"nemlig.com rejected the request ({resp.status_code}).")
        try:
            return resp.json() if resp.content else None
        except ValueError as exc:
            raise StoreError(
                "nemlig.com did not return JSON. Its API may have changed."
            ) from exc

    async def _api(self, method: str, url: str, **kwargs: Any) -> Any:
        """A logged-in call. On 401: renew the Bearer token, then log in again."""
        async with self._lock:
            if not self._logged_in:
                await self._login()
        for renew in (self._renew_token, self._login, None):
            try:
                return await self._send(method, url, **kwargs)
            except _Unauthorized:
                if renew is None:
                    bad = StoreError("nemlig.com rejected the login. Check your email and password.")
                    raise (expired(self.name) if self._resumed else bad) from None
                try:
                    await renew()
                except _Unauthorized:
                    continue
        raise AssertionError("unreachable")

    def _has_login(self) -> bool:
        """Logged in, or a login is saved. Once logged in the keychain is not read again."""
        return self._logged_in or has_login(self.name)

    async def _public(self, method: str, url: str, **kwargs: Any) -> Any:
        """A call that needs no login (offers, settings): logged in when a login is saved, so
        prices and delivery time fit the household; else with an anonymous token, renewed
        once when nemlig says it has expired. Search is not such a call: with an anonymous
        token nemlig's search gateway answers 500 (live 2026-11-04)."""
        if self._has_login():
            return await self._api(method, url, **kwargs)
        for last in (False, True):
            try:
                async with self._lock:
                    if not self._bearer:
                        await self._renew_token()
                return await self._send(method, url, **kwargs)
            except _Unauthorized:
                if last:
                    raise StoreError(
                        "nemlig.com rejected the request without a login. Save one with: handle-mcp login nemlig"
                    ) from None
                self._bearer = None  # expired: the next round fetches a fresh one
        raise AssertionError("unreachable")

    async def _renew_token(self) -> None:
        self._bearer = None
        token = await self._send("GET", f"{BASE}/webapi/Token")
        self._bearer = token["access_token"]
        xsrf = await self._send("GET", f"{BASE}/webapi/AntiForgery")
        self._xsrf = xsrf["Value"]

    async def _login(self) -> None:
        login = load_login(self.name)
        self._bearer = self._xsrf = None
        if "session" in login:  # saved by `handle-mcp login`: no password (docs/adr/0003)
            return await self._resume(login["session"])
        try:
            await self._renew_token()
            result = await self._send(
                "POST",
                f"{BASE}/webapi/login",
                json={"Username": login["email"], "Password": login["password"]} | LOGIN_FLAGS,
            )
        except _Unauthorized:
            raise StoreError("nemlig.com rejected the login. Check your email and password.") from None
        if not isinstance(result, dict) or "RedirectUrl" not in result:
            raise StoreError("nemlig.com login failed. Check your email and password.")
        await self._renew_token()  # tokens change after login
        self._logged_in = True
        log.info("logged in to nemlig.com")
        if session := self.session():  # an old saved password is replaced by the session
            save_session(self.name, login["email"], session)

    async def _resume(self, session: dict) -> None:
        """Log in with the saved login cookie. A dead cookie gives an anonymous basket
        (no email), not a 401: check it, never shop anonymously by mistake."""
        if not session.get(AUTH_COOKIE):
            raise expired(self.name)
        self._client.cookies.set(AUTH_COOKIE, session[AUTH_COOKIE], domain=".nemlig.com")
        try:
            await self._renew_token()
            basket = await self._send("GET", f"{BASE}/webapi/basket/GetBasket")
        except _Unauthorized:
            raise expired(self.name) from None
        if not (basket or {}).get("Email"):
            raise expired(self.name)
        self._logged_in = self._resumed = True
        log.info("resumed the nemlig.com session")

    def session(self) -> dict[str, str] | None:
        """The login cookie (1 year), saved instead of the password."""
        value = next((c.value for c in self._client.cookies.jar if c.name == AUTH_COOKIE), None)
        return {AUTH_COOKIE: value} if value else None

    async def _search_settings(self) -> dict[str, Any]:
        """Timestamp, timeslot and zone for search, from /webapi/ only: page URLs such as
        /?GetAsJson=1 can land in the queue-it waiting room. Defaults if a call fails."""
        logged = self._has_login()  # once per search: the keychain is not free
        fresh = self._settings and time.monotonic() - self._settings[0] < SETTINGS_TTL
        if fresh and (self._settings[2] or not logged):  # anonymous values die on login
            return self._settings[1]
        app, basket = {}, {}
        try:
            app = await self._public("GET", f"{BASE}/webapi/v2/AppSettings/Website") or {}
            if logged:  # without a login, nemlig answers for a default zone and time
                basket = await self._basket()
        except StoreError as exc:  # search still works, only less exact
            log.warning("nemlig: retrying with default search parameters: %s", exc)
        settings = {
            "timestamp": app.get("CombinedProductsAndSitecoreTimestamp", ""),
            "timeslotUtc": basket.get("TimeslotUtc", ""),
            "deliveryZoneId": basket.get("DeliveryZoneId") or 1,
        }
        if basket or (app and not logged):  # a failed call is tried again next time
            self._settings = (time.monotonic(), settings, bool(basket))
            self._slot_id = str((basket.get("DeliveryTimeSlot") or {}).get("Id") or "")
        return settings

    async def _timeslot_id(self) -> str:
        """The delivery time in the basket, so recipe products and offers fit it (sold out on
        that day, prices). Empty if none: nemlig then answers for a default time."""
        await self._search_settings()
        return self._slot_id

    # --- Store methods ----------------------------------------------------

    async def search(self, query: str, limit: int = 10) -> list[Product]:
        if not self._has_login():
            raise StoreError(
                "nemlig product search requires a login (offers do not). Save a login with: handle-mcp login nemlig"
            )
        params = {"query": query, "take": limit, "skip": 0, "recipeCount": 0}
        params |= await self._search_settings()
        data = await self._api("GET", SEARCH, params=params)
        try:
            hits = data["Products"]["Products"]
        except (KeyError, TypeError) as exc:
            raise StoreError("nemlig product search has changed format.") from exc
        return [self._product(h) for h in hits]

    def _product(self, hit: dict) -> Product:
        avail = hit.get("Availability") or {}
        campaign = hit.get("Campaign") or None
        price = _money(hit.get("Price")) or Decimal(0)
        now = _discount_price(campaign)
        if now is not None and now >= price:  # seen live: a "Dagstilbud" above the normal price
            now = None
        offer = _offer(campaign) if now is not None or not _discount_price(campaign) else None
        if (campaign or {}).get("Type") == FREE and now is not None:
            offer = "Free"
        elif offer == "Offer" and now is not None:
            offer = f"Offer, was {_kr(price)} DKK"
        limit = (campaign or {}).get("MaxQuantity") or None
        if offer and limit:
            offer += f" (max {limit} stk.)"
        return Product(
            store=self.name,
            id=str(hit["Id"]),
            name=hit.get("Name", ""),
            price=price if now is None else now,
            brand=hit.get("Brand") or None,
            size=hit.get("Description") or None,
            unit_price=_money(campaign.get("CampaignUnitPrice"))
            if now is not None
            else _money(hit.get("UnitPriceCalc")),
            unit=_unit(hit.get("UnitPriceLabel")),
            ean=None,  # not in nemlig's search answers (docs/research.md)
            on_offer=bool(offer),
            offer_text=offer,
            before_price=price if now is not None else None,
            offer_until=_until(campaign),
            available=avail.get("IsDeliveryAvailable", True)
            and avail.get("IsAvailableInStock", True),
            category=hit.get("Category") or None,
            campaign="Free"
            if offer and offer.startswith("Free")
            else hit.get("CampaignAttribute"),
            max_per_order=limit if offer else None,
        )

    async def _basket(self) -> dict:
        return self._own(await self._api("GET", f"{BASE}/webapi/basket/GetBasket") or {})

    def _own(self, basket: dict) -> dict:
        """A saved cookie that dies mid-run gives an anonymous basket, not a 401: stop there."""
        if self._resumed and not basket.get("Email"):
            raise expired(self.name)
        return basket

    def _cart(self, basket: dict) -> Cart:
        lines = [
            CartLine(
                str(ln["Id"]),
                ln.get("Name", ""),
                int(ln.get("Quantity", 0)),
                _money(ln.get("Price")) or Decimal(0),
            )
            for ln in basket.get("Lines") or []
            if int(ln.get("Quantity") or 0) > 0  # removed lines can stay with a price
        ]
        products = _money(basket.get("TotalProductsPrice"))
        if products is None:
            products = sum((ln.line_total for ln in lines), Decimal(0))
        fees = {
            name: _money(basket.get(key))
            for name, key in (
                ("levering", "DeliveryPrice"),
                ("poser", "TotalBagsPrice"),
                ("pant", "TotalDepositsPrice"),
            )
            if basket.get(key)
        }
        total = _money(basket.get("TotalPrice"))
        warnings = [
            w.get("Message", str(w)) if isinstance(w, dict) else str(w)
            for w in basket.get("ValidationFailures") or []
        ]
        return Cart(
            store=self.name,
            delivery_mode="home",
            lines=lines,
            products_total=products,
            fees=fees,
            total=total if total is not None else products + sum(fees.values(), Decimal(0)),
            minimum_order=_money(basket.get("MinimumOrderTotal")),
            minimum_met=basket.get("IsMinTotalValid"),
            delivery=basket.get("FormattedDeliveryTime") or None,
            warnings=warnings,
        )

    async def get_cart(self) -> Cart:
        return self._cart(await self._basket())

    async def set_quantities(self, items: dict[str, int]) -> list[QuantityResult]:
        if self._resumed:
            await self._basket()  # never fill an anonymous basket
        results = []
        for product_id, quantity in items.items():
            payload = {
                "ProductId": product_id,
                "quantity": quantity,
                "AffectPartialQuantity": False,
                "disableQuantityValidation": False,
            }
            try:
                basket = (
                    await self._api("POST", f"{BASE}/webapi/basket/AddToBasket", json=payload) or {}
                )
            except StoreError as exc:
                results.append(QuantityResult(product_id, quantity, None, False, str(exc)))
                continue
            applied = next(
                (
                    int(ln.get("Quantity", 0))
                    for ln in basket.get("Lines") or []
                    if str(ln.get("Id")) == product_id
                ),
                0,
            )
            ok = applied == quantity
            message = None if ok else f"nemlig set the quantity to {applied}, not {quantity}."
            results.append(QuantityResult(product_id, quantity, applied, ok, message))
        return results

    async def terms(self) -> StoreTerms:
        basket = await self._basket()
        bags = _money(basket.get("TotalBagsPrice"))
        empty = not any(int(ln.get("Quantity") or 0) > 0 for ln in basket.get("Lines") or [])
        return StoreTerms(
            store=self.name,
            delivery_mode="home",
            home_delivery=True,
            delivery_fee=_money(basket.get("DeliveryPrice")),
            other_fees={"bags (estimated)": _bag_fee()}
            if empty
            else {"poser": bags}
            if bags
            else {},
            minimum_order=_money(basket.get("MinimumOrderTotal")),
        )

    def _order(self, o: dict, lines: list[CartLine] | None = None) -> Order:
        delivery = o.get("DeliveryTime") or {}
        return Order(
            store=self.name,
            id=str(o.get("Id", "")),
            status=STATUS.get(o.get("Status"), f"status {o.get('Status')}"),
            total=_money(o.get("Total")) or Decimal(0),
            placed_at=_time(o.get("OrderDate")),
            delivery_start=_time(delivery.get("Start")),
            delivery_end=_time(delivery.get("End")),
            lines=lines or [],
        )

    async def orders(self, limit: int = 10) -> list[Order]:
        data = await self._api(
            "GET", f"{BASE}/webapi/order/GetBasicOrderHistory", params={"skip": 0, "take": limit}
        )
        return [self._order(o) for o in (data or {}).get("Orders", [])]

    async def order(self, order_id: str) -> Order:
        if not re.fullmatch(r"[0-9]{1,20}", order_id):  # it goes into the URL path
            raise StoreError("The order number must be numeric, as returned by get_orders.")
        data = await self._api("GET", f"{BASE}/webapi/v2/order/GetOrderHistory/{order_id}") or {}
        lines = [
            CartLine(
                str(ln.get("ProductNumber", "")),
                ln.get("ProductName", ""),
                int(ln.get("Quantity", 0)),
                _money(ln.get("Amount")) or Decimal(0),
            )
            for ln in data.get("Lines") or []
        ]
        if not lines and not data.get("Id"):
            raise StoreError(f"Order {order_id} was not found at nemlig.")
        meta = {"Id": order_id, **{k: v for k, v in data.items() if k != "Lines"}}
        order = self._order(meta, lines)
        if not order.total:
            order.total = sum((ln.line_total for ln in lines), Decimal(0))
        return order


class _Unauthorized(Exception):
    """HTTP 401 from nemlig.com: the Bearer token or the login cookie expired."""
