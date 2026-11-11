"""What an item costs in one store: packs for the amount, offers and the delivery day.

- A size like "1,5 kg" is the amount wanted: a 500 g pack is bought 3 times. Never less than
  wanted (990 g is 1 kg, 900 g is not); up to 15 % more still counts as the same size.
- "6 x bananer" with no size means 6 bananas: a 4-pack is bought twice.
- An offer is gone after its last day, so prices are for the delivery day.
- A multi-buy offer ("3 for 48 kr") counts for the packs we need, also in the price per kg.
  Packs over the offer's limit ("max 1 stk.") cost the normal price.
- No size in the item: price per kg, l or piece, so packs of different sizes compare.
"""

from __future__ import annotations

import math
import re
from datetime import date
from decimal import Decimal
from typing import Any

from handle_mcp.match import PIECES, get, pack_size

SIZE_TOLERANCE = Decimal("0.15")  # up to 15 % MORE than wanted still counts as the same size

SHORT_TOLERANCE = Decimal("0.02")  # never less than wanted: 990 g is 1 kg, 900 g is not

MULTI_RE = re.compile(r"(\d+)\s*for\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)  # "4 for 50 kr"


def _price(product: Any, on: date | None) -> Decimal:
    """The price on the delivery day: the offer is gone after its last day."""
    until, before = get(product, "offer_until"), get(product, "before_price")
    if on and until and before and on > until:
        return Decimal(before)
    return Decimal(get(product, "price"))


def _multi(product: Any, on: date | None) -> tuple[int, Decimal] | None:
    """A multi-buy offer like '4 for 50 kr' that is still on: (4, 50)."""
    match = MULTI_RE.search(get(product, "offer_text") or "")
    until = get(product, "offer_until")
    if not match or (on and until and on > until):
        return None
    size, total = int(match.group(1)), Decimal(match.group(2).replace(",", "."))
    return (size, total) if size > 1 and total < _price(product, on) * size else None


def cost(product: Any, count: int, on: date | None) -> Decimal:
    """Price for `count` packs, with a multi-buy offer like '4 for 50 kr' when it is still on.
    Packs over the offer's limit ('max 1 stk.', e.g. a free gift) cost the normal price."""
    price = _price(product, on)
    limit, before = get(product, "max_per_order"), get(product, "before_price")
    offered = min(count, limit) if limit else count  # the limit caps multi-buys too
    normal = Decimal(before) if before else price
    if offer := _multi(product, on):
        size, total = offer
        cost = (offered // size) * total + (offered % size) * price
    else:
        cost = price * offered
    return cost + normal * (count - offered)


def multi_hint(product: Any, count: int, on: date | None) -> str | None:
    """Say it when the multi-buy is close: 2 packs at 22 kr, but '3 for 48 kr' is 4 kr more.
    We never buy more than needed on our own: the human decides."""
    offer = _multi(product, on)
    if not offer or count % offer[0] == 0:
        return None
    full = math.ceil(count / offer[0]) * offer[0]
    extra = cost(product, full, on) - cost(product, count, on)
    if extra >= _price(product, on):
        return None
    return f"Offer {get(product, 'offer_text')}: {full - count} more for {extra} DKK."


def per_unit(product: Any, quantity: int, count: int, on: date | None) -> tuple[int, Decimal]:
    """For an item with no size: compare price per kg, l or piece, not per pack.
    '4 x pærer' counts pieces, so pieces come first. 'æbler' is a bag, so weight comes first.
    The pack price is what `count` packs cost each, so '3 for 48 kr' counts for 3 packs."""
    have = pack_size(product)
    price = cost(product, count, on) / count
    if not have or not have[1]:
        return 2, price
    by_piece = have[0] in PIECES
    kind = (0 if by_piece else 1) if quantity > 1 else (1 if by_piece else 0)
    return kind, price / have[1]


def packs(wanted: tuple[str, Decimal] | None, quantity: int, product: Any) -> int | None:
    """How many packs give the wanted amount. '1,5 kg' with 500 g packs → 3.
    None when the sizes cannot be compared (no size, or kg against stk)."""
    have = pack_size(product)
    # '8 stk' toilet paper takes rolls. '8 ruller' never takes '42 stk' wet wipes.
    unit = "stk" if have and have[0] == "rl" and wanted and wanted[0] == "stk" else have and have[0]
    if not wanted or not have or wanted[0] != unit or not have[1]:
        return None
    need = wanted[1] * quantity * (1 - SHORT_TOLERANCE)
    return max(1, math.ceil(need / have[1]))


def pieces(wanted: tuple[str, Decimal] | None, quantity: int, product: Any) -> int | None:
    """'6 x bananer' with no size means 6 bananas: a 4-pack is bought twice, not 6 times."""
    have = pack_size(product)
    if wanted or quantity < 2 or not have or have[0] not in PIECES or have[1] <= 1:
        return None
    return math.ceil(quantity / have[1])


def too_much(wanted: tuple[str, Decimal], quantity: int, count: int, product: Any) -> bool:
    """True when the packs give over 15 % more than wanted: 500 g for '400 g'."""
    have = pack_size(product)
    return bool(have) and have[1] * count > wanted[1] * quantity * (1 + SIZE_TOLERANCE)
