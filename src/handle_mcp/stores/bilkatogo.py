"""BilkaToGo: the private API bilkatogo.dk uses. See docs/research.md.

Login: Gigya accounts.login → accounts.getJWT (24 h) → POST api.bilkatogo.dk/api/auth/LoginJWT.
Search goes to the website's public Algolia index. Money comes in øre.
Facts and fixtures from nickknissen/bilkatogo (MIT, Copyright (c) 2026 Nick Nissen).
Never add order, payment or delivery-address calls here (docs/adr/0001).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from handle_mcp.models import (
    Cart,
    CartLine,
    DeliveryInfo,
    DeliverySlot,
    Order,
    Product,
    QuantityResult,
    StoreTerms,
)
from handle_mcp.stores import StoreError, expired, has_login, load_login, save_session
from handle_mcp.stores._http import request
from handle_mcp.stores.bilkatogo_algolia import ALGOLIA, ALGOLIA_HEADERS, ALGOLIA_INDEX
from handle_mcp.stores.bilkatogo_nutrition import BilkaNutritionMixin
from handle_mcp.stores.bilkatogo_recipes import BilkaRecipesMixin

log = logging.getLogger(__name__)

GIGYA = "https://accounts.eu1.gigya.com"
# Public keys from bilkatogo.dk's own JavaScript. Not secrets: every visitor's browser sends them.
GIGYA_API_KEY = "3_tA6BbV434FQqN73HnUG1KA3qFv8KiG4OqLu9eWPh7sKRqRizH5Vfv5Larmgrb4I2"
GIGYA_BAD_LOGIN = 403042
GIGYA_RATE_LIMIT = 403048  # with 5xxxxx (Gigya's server errors): try later, the login is fine
API = "https://api.bilkatogo.dk/api"
JWT_SECONDS = 86400
FEE_GROUPS = {"Emballage", "Services"}  # cart groups that are fees, not products
TZ = ZoneInfo("Europe/Copenhagen")
UNITS = {"kg": "kg", "ltr": "l", "l": "l", "stk": "stk"}
STATUS = {  # Bilka's codes → the words the website shows
    "Bestilt": "Modtaget",
    "Ubehandlet": "Under behandling",
    "Pakket": "Pakket",
    "Leveret": "Leveret",
    "Faktureret": "Leveret",
    "Annulleret": "Annulleret",
}
UNKNOWN_MODE = (
    "BilkaToGo does not indicate whether the cart is set to home delivery or pickup. "
    "Choose this on bilkatogo.dk."
)


GENERIC_OFFERS = {"Se Her"}  # offerDescription with no real text (docs/research.md)


def _kr(ore: Any) -> Decimal:
    try:
        return Decimal(str(ore or 0)).scaleb(-2)
    except (InvalidOperation, ValueError) as exc:
        raise StoreError(f"BilkaToGo returned an unknown amount: {ore!r}.") from exc


def _campaign(offer: str | None) -> str | None:
    """'Skarp pris' stays, 'Mix 10 for 25,00' is 'Mix', leaflet offers are 'Tilbud'."""
    if not offer:
        return None
    return offer if offer == "Skarp pris" else "Mix" if offer.startswith("Mix ") else "Offer"


def _today() -> date:
    return datetime.now(TZ).date()


def _is_home(address: dict) -> bool:
    """Home addresses have a DAWA id or a recipient; pickup points (Bilka stores) do not."""
    return bool(address.get("dawa_uid") or address.get("recipient_name"))


def _mode(address: dict) -> tuple[str | None, str | None]:
    """("home", None), ("pickup", "Bilka Vejle") or (None, None). Fails closed (docs/adr/0006):
    on a home cart `name` is the customer's, so it is read only when nothing says home."""
    if _is_home(address):
        return "home", None
    name = str(address.get("name") or "").strip()
    return ("pickup", name) if address.get("sid") and name.startswith("Bilka") else (None, None)


def _slot(day: str | None, start: str | None, end: str | None) -> tuple[datetime | None, ...]:
    try:
        d = date.fromisoformat(day or "")
        return tuple(datetime.combine(d, time.fromisoformat(t), TZ) for t in (start, end))
    except (TypeError, ValueError):
        return None, None


def _slot_ended(data: dict) -> bool:
    """Bilka keeps the slot of the last order in the cart ("Ons. 04/09" seen weeks later).
    True only when the slot parses and has ended; an unreadable slot is kept, not hidden."""
    chosen = data.get("deliveryDate") or {}
    _start, end = _slot(
        chosen.get("deliveryDate"), chosen.get("intervalStart"), chosen.get("intervalEnd")
    )
    return bool(end and end < datetime.now(TZ))


def _chosen_slot(data: dict) -> tuple[datetime | None, datetime | None]:
    """The delivery time chosen in the cart; none when it has ended."""
    if _slot_ended(data):
        return None, None
    chosen = data.get("deliveryDate") or {}
    return _slot(chosen.get("deliveryDate"), chosen.get("intervalStart"), chosen.get("intervalEnd"))


def _lines(groups: list[dict]) -> list[CartLine]:
    """Cart and order lines are nested: groups → blocks → orderlines."""
    return [
        CartLine(
            str((ln.get("product") or {}).get("objectID", "")),
            (ln.get("product") or {}).get("name", ""),
            int(ln.get("quantity") or 0),
            _kr(ln.get("amount")),
        )
        for group in groups or []
        if group.get("headline") not in FEE_GROUPS
        for block in group.get("lines") or []
        for ln in block.get("orderlines") or []
    ]


class _Refused(StoreError):
    """Bilka answered HTTP 200 with an error body."""


class _GigyaRefused(StoreError):
    """Gigya answered with an errorCode: bad login or a dead login_token."""


class BilkaStore(BilkaRecipesMixin, BilkaNutritionMixin):
    name = "bilkatogo"
    cart_page = "https://www.bilkatogo.dk/kurv"  # the page the human opens to see the cart and pay

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=30)
        self._jwt: str | None = None
        self._token: str | None = None  # Gigya login_token: saved instead of the password
        self._jwt_until = datetime.min.replace(tzinfo=TZ)
        self._lock = asyncio.Lock()

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return await request(self._client, method, url, "BilkaToGo", **kwargs)

    @staticmethod
    def _json(resp: httpx.Response, what: str) -> dict:
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise StoreError(f"BilkaToGo ({what}) returned an unexpected response. Its API may have changed.")
        return data

    async def _gigya(self, method: str, form: dict[str, str]) -> dict:
        data = self._json(await self._send("POST", f"{GIGYA}/{method}", data=form), method)
        if data.get("errorCode") == GIGYA_BAD_LOGIN:
            raise _GigyaRefused("BilkaToGo rejected the login. Check your email and password.")
        code = data.get("errorCode", 0)
        if code == GIGYA_RATE_LIMIT or (isinstance(code, int) and code >= 500000):
            raise StoreError("BilkaToGo login is temporarily unavailable. Try again shortly.")
        if code != 0:
            raise _GigyaRefused(
                f"BilkaToGo login failed: {data.get('errorMessage', data['errorCode'])}"
            )
        return data

    async def _login(self) -> None:
        login = load_login(self.name)
        if "session" in login:  # saved by `handle-mcp login`: no password (docs/adr/0003)
            token = login["session"].get("login_token")
            if not token:
                raise expired(self.name)
            try:
                jwt = await self._jwt_for(token)
            except _GigyaRefused:  # Gigya refuses the token: the session is over
                raise expired(self.name) from None
        else:
            form = {"loginID": login["email"], "password": login["password"]}
            form |= {"apiKey": GIGYA_API_KEY, "httpStatusCodes": "true"}
            session = (await self._gigya("accounts.login", form)).get("sessionInfo")
            token = (session or {}).get("cookieValue")
            jwt = await self._jwt_for(token) if token else None
        if not token or not jwt:
            raise StoreError("BilkaToGo login returned no token. Its API may have changed.")
        # Never send the `cart` cookie: LoginJWT would REPLACE the account's cart with it.
        self._client.cookies.delete("cart")
        resp = await self._send(
            "POST", f"{API}/auth/LoginJWT", params={"u": "w"}, headers={"jwt_token": jwt}
        )
        if resp.status_code != 200:
            raise StoreError(f"BilkaToGo login failed ({resp.status_code}).")
        self._jwt, self._token = jwt, token
        self._jwt_until = datetime.now(TZ) + timedelta(seconds=JWT_SECONDS - 300)
        log.info("logged in to BilkaToGo")
        if "password" in login:  # an old saved password is replaced by the session
            save_session(self.name, login["email"], {"login_token": token})

    async def _jwt_for(self, token: str) -> str | None:
        form = {"apiKey": GIGYA_API_KEY, "login_token": token, "expiration": str(JWT_SECONDS)}
        return (await self._gigya("accounts.getJWT", form)).get("id_token")

    def session(self) -> dict[str, str] | None:
        return {"login_token": self._token} if self._token else None

    async def _shop(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict:
        for fresh in (False, True):
            used = self._jwt
            async with self._lock:
                # Skip a fresh login if another call already logged in while we waited.
                stale = fresh and self._jwt == used
                if stale or not self._jwt or datetime.now(TZ) >= self._jwt_until:
                    await self._login()
            resp = await self._send(
                method,
                f"{API}/{path}",
                params={"u": "w", **(params or {})},
                headers={"jwt_token": self._jwt or ""},
            )
            if resp.status_code == 401:
                continue
            if resp.status_code >= 400:
                raise StoreError(f"BilkaToGo rejected {path} ({resp.status_code}).")
            data = self._json(resp, path)
            if data.get("code") == 1:  # {"code": 1, "msg": "You are not logged in"}
                continue
            if "msg" in data and "code" in data:
                raise _Refused(data["msg"])
            return data
        raise StoreError("BilkaToGo rejected a fresh session. Check your login.")

    async def _cart_json(self) -> dict:
        return await self._shop("GET", "shop/v6/Cart", {"extra": "deliveryAddress,deliveryDate"})

    async def search(self, query: str, limit: int = 10) -> list[Product]:
        return await self._algolia(query, limit)

    async def offers(self, limit: int = 25) -> list[Product]:
        """Products on offer in our store: Skarp pris, leaflet offers and mix offers."""
        return await self._algolia("", limit, offers=True)

    async def _algolia(
        self, query: str, limit: int, offers: bool = False, ids: list[str] | None = None
    ) -> list[Product]:
        """Search, offers, or `ids`: the products with those Bilka item numbers (recipes)."""
        if not has_login(self.name):  # the assortment, prices and offers follow the address
            raise StoreError(
                "BilkaToGo shows prices and offers for the store associated with your delivery address, "
                "so a login is required. Run: handle-mcp login bilkatogo"
            )
        address = (await self._cart_json()).get("deliveryAddress") or {}
        sid = str(address.get("sid") or "")
        if not sid:
            raise StoreError(
                "No delivery address is selected in BilkaToGo. Choose home delivery or pickup on bilkatogo.dk."
            )
        filters = f"isInAssortmentIn:{sid} AND nonsearchable:false"
        if offers:  # isInOffer is a list of store ids, not true/false
            filters = f"isInOffer:{sid} AND {filters}"
        if ids:  # a recipe's products may be hidden from search: no nonsearchable filter
            filters = f"({' OR '.join(f'objectID:{i}' for i in ids)}) AND isInAssortmentIn:{sid}"
        params = urlencode(
            {
                "query": query,
                "hitsPerPage": limit,
                # Algolia needs every word by default: "cheasy skyr passionsfrugt" gives 0 hits
                # when that flavour is not sold. Then retry with fewer words, like nemlig does.
                "removeWordsIfNoResults": "allOptional",
                "filters": filters,
                "attributesToRetrieve": json.dumps(
                    [
                        "name",
                        "brand",
                        "gtin",
                        "netcontent",
                        "productType",
                        "deepestCategoryPath",
                        "price",  # the price without the offer
                        f"storeData.{sid}",
                    ]
                ),
            }
        )
        body = {"requests": [{"indexName": ALGOLIA_INDEX, "params": params}]}
        resp = await self._send("POST", ALGOLIA, headers=ALGOLIA_HEADERS, json=body)
        what = "offers" if offers else "search"
        if resp.status_code != 200:
            raise StoreError(f"BilkaToGo {what} failed ({resp.status_code}).")
        try:
            hits = self._json(resp, what)["results"][0]["hits"]
        except (KeyError, IndexError, TypeError) as exc:
            raise StoreError(f"BilkaToGo-{what} response format has changed.") from exc
        return [p for p in (self._product(h, sid) for h in hits) if p]

    def _product(self, hit: dict, sid: str) -> Product | None:
        store = (hit.get("storeData") or {}).get(sid)
        if not store:
            return None
        unit = (store.get("unitsOfMeasurePriceUnit") or "").rstrip(".").lower()
        now, normal = _kr(store.get("price")), _kr(hit.get("price"))
        before = normal if normal > now else None
        offer = store.get("offerDescription") or None
        if offer in GENERIC_OFFERS:  # a leaflet offer with no text of its own
            offer = f"Offer, was {before:.2f} DKK".replace(".", ",") if before else "Offer"
        shown = store.get(
            "unitsOfMeasureShowPrice"
        )  # with the offer; unitsOfMeasurePrice is without
        return Product(
            store=self.name,
            id=str(hit["objectID"]),
            name=hit.get("name", ""),
            price=now,  # what you pay now, offer included
            brand=hit.get("brand") or None,
            size=hit.get("netcontent") or None,
            unit_price=_kr(shown or store.get("unitsOfMeasurePrice")) if unit else None,
            unit=UNITS.get(unit, unit or None),
            ean=hit.get("gtin") or None,
            on_offer=bool(offer),
            offer_text=offer,
            before_price=before if offer else None,
            campaign=_campaign(offer),
            max_per_order=(int(store.get("offerMax") or 0) or None) if offer else None,
            available=bool(store.get("inStock")),
            category=" > ".join(
                x for x in (hit.get("deepestCategoryPath"), hit.get("productType")) if x
            )
            or None,
        )

    def _cart(self, data: dict) -> Cart:
        stat, address = data.get("stat") or {}, data.get("deliveryAddress") or {}
        lines = _lines(data.get("lines"))
        fees = {
            name: _kr(stat.get(key))
            for name, key in (
                ("levering", "delivery_price"),
                ("pakkeservice", "packing"),
                ("emballage", "packaging_price"),
                ("pant", "deposit"),
            )
            if stat.get(key)
        }
        chosen_text = (data.get("deliveryDate") or {}).get("delivery_message") or None
        if _slot_ended(data):
            chosen_text = None  # the slot of a past order is no choice
        mode, pickup_store = _mode(address)
        warnings = [] if mode else [UNKNOWN_MODE]
        if stat.get("minimum_left_text"):
            warnings.append(re.sub(r"<[^>]+>", "", stat["minimum_left_text"]))
        warnings += [
            m.get("text", str(m)) if isinstance(m, dict) else str(m)
            for m in data.get("messages") or []
        ]
        return Cart(
            store=self.name,
            lines=lines,
            products_total=sum((ln.line_total for ln in lines), Decimal(0)),
            fees=fees,
            total=_kr(stat.get("price")),
            minimum_order=_kr(address["minimum_buy"]) if address.get("minimum_buy") else None,
            minimum_met=not stat.get("minimum_left"),
            delivery=chosen_text,
            warnings=warnings,
            delivery_mode=mode,
            pickup_store=pickup_store,
        )

    async def get_cart(self) -> Cart:
        return self._cart(await self._cart_json())

    async def set_quantities(self, items: dict[str, int]) -> list[QuantityResult]:
        results = []
        for pid, qty in items.items():
            try:
                answer = await self._shop(
                    "GET",
                    "shop/v6/ChangeLineCount",
                    {"productId": pid, "count": qty, "fullCart": 0},
                )
            except _Refused as exc:  # Bilka's own message; other errors stop the loop
                results.append(QuantityResult(pid, qty, None, False, str(exc)))
                continue
            applied = answer.get("productAmount")
            note = (
                " ".join(m for m in (answer.get("message"), answer.get("offerLimitMessage")) if m)
                or None
            )
            ok = applied == qty
            if not ok:
                note = note or f"BilkaToGo set the quantity to {applied}, not {qty}."
            results.append(
                QuantityResult(pid, qty, applied if isinstance(applied, int) else None, ok, note)
            )
        return results

    async def terms(self) -> StoreTerms:
        data = await self._cart_json()
        cart = self._cart(data)
        return StoreTerms(
            store=self.name,
            home_delivery=cart.delivery_mode == "home",
            delivery_fee=self._fee(data, cart),
            other_fees={k: v for k, v in cart.fees.items() if k != "levering"},
            minimum_order=cart.minimum_order,
            delivery_mode=cart.delivery_mode,
            pickup_store=cart.pickup_store,
        )

    def _fee(self, data: dict, cart: Cart) -> Decimal | None:
        """Pickup is often free: a delivery_price of 0 is a known fee, not a missing one."""
        stat = data.get("stat") or {}
        if cart.delivery_mode == "pickup" and stat.get("delivery_price") is not None:
            return _kr(stat["delivery_price"])
        return cart.fees.get("levering")

    async def delivery_info(self) -> DeliveryInfo:
        data = await self._cart_json()
        address = data.get("deliveryAddress") or {}
        start, end = _chosen_slot(data)
        nxt = address.get("nextAvailableDeliveryDate")
        mode, pickup_store = _mode(address)
        return DeliveryInfo(
            store=self.name,
            chosen_start=start,
            chosen_end=end,
            next_available=date.fromisoformat(nxt) if nxt else None,
            note=None if mode else UNKNOWN_MODE,
            delivery_mode=mode,
            pickup_store=pickup_store,
        )

    async def delivery_slots(self, days: int = 7) -> list[DeliverySlot]:
        """GET shop/v3/AvailableDeliveryDates for the cart's address or pickup store. Bilka has no
        price per slot: the fee is the cart's flat one. Never choose a slot, store or mode here."""
        data, cart = await asyncio.gather(
            self._shop("GET", "shop/v3/AvailableDeliveryDates"), self._cart_json()
        )
        parsed = self._cart(cart)
        if parsed.delivery_mode is None:
            raise StoreError(UNKNOWN_MODE)
        fee, pickup = self._fee(cart, parsed), parsed.delivery_mode == "pickup"
        unknown = (
            "" if fee is not None else "The fee is unknown: it is not shown in the cart (empty cart?). "
        )
        last = _today() + timedelta(days=days - 1)  # `days` calendar days, today included
        slots = []
        for day in data.get("availableIntervals") or []:
            for iv in day.get("intervals") or []:
                start, end = _slot(day.get("date"), iv.get("intervalStart"), iv.get("intervalEnd"))
                if start is None or end is None or start.date() > last:
                    continue
                info = " ".join(m.get("message", "") for m in iv.get("info") or [])
                slots.append(
                    DeliverySlot(
                        store=self.name,
                        start=start,
                        end=end if end > start else end + timedelta(days=1),
                        price=fee,
                        available=not iv.get("closed")
                        and bool(1 & (iv.get("availability_bit") or 0)),
                        kind="Pickup" if pickup else "Night" if start.hour < 6 else None,
                        note=(unknown + re.sub(r"<[^>]+>", "", info)).strip() or None,
                    )
                )
        return slots

    def _order(self, o: dict, lines: list[CartLine] | None = None) -> Order:
        start, end = _slot(
            o.get("deliveryDate"),
            *((o.get("deliveryInterval") or {}).get(k) for k in ("start", "end")),
        )
        ordered = o.get("ordered") or {}
        placed = (
            f"{ordered.get('date')}T{ordered.get('time') or '00:00:00'}"
            if ordered.get("date")
            else None
        )
        return Order(
            store=self.name,
            id=str(o.get("orderId", "")),
            status=STATUS.get(o.get("status", ""), o.get("status") or "ukendt"),
            total=_kr(o.get("packedPrice") or o.get("orderedPrice") or o.get("price")),
            placed_at=datetime.fromisoformat(placed).replace(tzinfo=TZ) if placed else None,
            delivery_start=start,
            delivery_end=end,
            lines=lines or [],
        )

    async def orders(self, limit: int = 10) -> list[Order]:
        params = {"limit": limit, "offset": 0, "muah": 1, "ordertype": "dibs,pgw"}
        data = await self._shop("POST", "shop/v7/OrderHistory", params)
        return [self._order(o) for o in data.get("orders") or []]

    async def order(self, order_id: str) -> Order:
        details = (await self._shop("GET", "shop/v7/OrderDetails", {"orderId": order_id})).get(
            "order"
        )
        if not details:
            raise StoreError(f"Order {order_id} was not found at BilkaToGo.")
        return self._order(details, _lines(details.get("lines")))
