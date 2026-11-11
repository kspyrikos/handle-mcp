"""nemlig.com offers: the lists on /tilbud, read from nemlig's new web API (productbff).

The page URL /tilbud?GetAsJson=1 can send us to the queue-it waiting room; the API does not
(seen live 2026-11-03). One call gives every list with its products, and needs no user id.
Shapes: tests/fixtures/nemlig/README.md.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any

from handle_mcp.models import Product
from handle_mcp.stores import StoreError
from handle_mcp.stores.nemlig_recipes import BFF

log = logging.getLogger(__name__)

# Offer lists on /tilbud, matched on the heading. The other lists on the page are categories.
OFFER_LISTS = {
    "skarp pris": "Skarp pris",
    "ugens priskup": "Priskup",
    "priskup": "Priskup",
    "dagstilbud": "Dagstilbud",
    "undgå madspild": "Undgå madspild",
    "en gave til dig": "Free",
}
UNITS = {"Weight": "kg", "Volume": "l", "Pieces": "stk"}
MULTI = re.compile(r"^(Mix )?\d+ stk\.")


def _kr(ore: Any) -> Decimal | None:
    return None if ore is None else Decimal(int(ore)) / 100


def _text(value: Decimal) -> str:
    return f"{value:.2f}".replace(".", ",").removesuffix(",00")


def offer_product(p: dict, campaign: str) -> Product | None:
    """One product from a list, or None if it is not cheaper than its normal price."""
    price = Decimal(int(p["price"])) / 100  # no price: format change, never "Gratis"
    before = _kr(p.get("priceOriginal"))
    if before is not None and price >= before:
        return None
    limit = p.get("labelMaxQuantity") or None
    multi = next(  # "Mix 2 stk. 35,-" or "2 stk. 15,-"
        (ln["text"] for ln in p.get("campaignLines") or [] if MULTI.match(ln.get("text") or "")),
        None,
    )
    if campaign == "Free" or (price == 0 and before):
        campaign, text = "Free", "Free"
    else:
        text = multi or (f"Offer, was {_text(before)} DKK" if before else campaign)
    if limit:
        text += f" (max {limit} stk.)"
    per_unit = p.get("pricePerUnit") or {}
    return Product(
        store="nemlig",
        id=str(p["id"]),
        name=p.get("title", ""),
        price=price,
        size=p.get("description") or None,
        unit_price=_kr(per_unit.get("value")),
        unit=UNITS.get(per_unit.get("unitType")),
        on_offer=True,
        offer_text=text,
        before_price=before,
        available=(p.get("availability") or {}).get("type", "Available") == "Available",
        campaign=campaign,
        max_per_order=limit,
    )


class OffersMixin:
    """offers() for NemligStore. Uses its `_public` (login optional) and `search`."""

    _public: Any
    _has_login: Any
    _timeslot_id: Any
    search: Any

    async def offers(self, limit: int = 25) -> list[Product]:
        """Every product on nemlig's offer lists (/tilbud): Skarp pris, Priskup, Dagstilbud,
        Undgå madspild and gifts. `limit` is per list. Read only. Works without a login:
        the offers are public; with one, they fit the household's delivery time."""
        params = {"path": "/tilbud"}
        if slot := await self._timeslot_id():
            params["timeslotId"] = slot  # sic: the page call spells it so, the dialog not
        page = await self._public("GET", f"{BFF}/page", params=params) or {}
        lists = [
            (OFFER_LISTS[head], block.get("products") or [])
            for block in page.get("pageContent") or []
            if (head := str((block.get("header") or {}).get("title") or "").strip().lower())
            in OFFER_LISTS
        ]
        if not lists:
            raise StoreError("No offer lists were found at nemlig. The website may have changed.")
        products: dict[str, Product] = {}
        broken = 0
        for name, items in lists:
            try:  # one broken list must not hide the others
                found = [o for p in items if (o := offer_product(p, name))][:limit]
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("nemlig offer list %s has changed format: %r", name, exc)
                broken += 1
                continue
            for product in found:
                if product:
                    products.setdefault(product.id, product)
        if broken == len(lists):
            raise StoreError("nemlig offer lists have changed format.")
        # "En gave til dig" is often empty (live 2026-11-02), but a search for "gratis" finds the
        # free gifts (ProductCampaignFreeProduct). A free-text search, so it may change.
        # Search needs a login; without one the lists above are the whole answer.
        gifts: list[Product] = []
        if self._has_login():
            try:
                gifts = [p for p in await self.search("gratis", 50) if p.campaign == "Free"]
            except StoreError as exc:
                log.warning("nemlig gift search failed: %s", exc)
        for gift in gifts:
            products.setdefault(gift.id, gift)
        return list(products.values())
