"""Candidates for one list item: the products the caller (an AI model or a human) picks from.

The matcher only suggests. Each candidate carries plain facts, so the caller can decide with
the user's own taste instead of hidden word rules: is 'Babymajs' acceptable, is organic worth it?
- The suggested product comes first, then the next best, at most LIMIT in all.
- An organic product is always among them when the search found one.
- Flags say what the matcher saw: name match, size fits, prepared, processed, blend and organic status.
  An unknown fact or a flag that is false is left out, to keep the answer small.
  `size_fits` is shown when false: the size does not fit.
"""

from __future__ import annotations

from typing import Any

from handle_mcp.match import (
    PIECES,
    asked,
    get,
    is_oeko,
    mixed,
    pack_size,
    parse_item,
    prepared,
    processed,
    score,
)
from handle_mcp.pricing import packs, too_much

LIMIT = 5

USUALLY_FALSE = {"suggested", "oeko", "prepared", "blend"}


def candidates(item: str, line: Any, others: list[Any]) -> list[dict[str, Any]]:
    """`line` is the suggested BasketLine, `others` the next best lines, best first."""
    if line.product is None:
        return []
    picked = others[: LIMIT - 1]
    if not is_oeko(line.product):
        oeko = [a for a in others if is_oeko(a.product)]
        if oeko and oeko[0] not in picked:
            picked = [*picked[: LIMIT - 2], oeko[0]]
    return [brief(item, line, suggested=True), *(brief(item, a) for a in picked)]


def brief(item: str, line: Any, suggested: bool = False) -> dict[str, Any]:
    """One product for the caller: what it costs for the amount, and the flags."""
    p = line.product
    quantity, words, size = parse_item(item)
    name_score = score(words, p, asked(item))
    fits = None
    if size:
        n = packs(size, quantity, p)  # None: no size, or kg against stk. Unknown, not false.
        fits = None if n is None else not too_much(size, quantity, line.quantity, p)
    elif quantity > 1 and (have := pack_size(p)) and have[0] in PIECES:
        fits = True  # '6 x bananer': pieces fit, a 1 kg bag cannot be compared
    facts = {
        "id": get(p, "id"),
        "name": get(p, "name"),
        "brand": get(p, "brand"),
        "size": get(p, "size"),
        "price": get(p, "price"),
        "quantity": line.quantity,
        "line_total": line.line_total,
        "unit_price": get(p, "unit_price"),  # per kg, l or stk, from the store
        "unit": get(p, "unit"),
        "offer_text": get(p, "offer_text"),
        "category": get(p, "category"),  # 'Konserves': a human (and the live list) sees canned
        "oeko": is_oeko(p),
        "note": line.note,
        "suggested": suggested,
        "name_match": "full" if name_score == 1 else "partial" if name_score >= 0.5 else "none",
        "size_fits": fits,  # None: the list gives no size
        "prepared": prepared(words, p),  # cold cuts, smoked, breaded
        "processed": processed(p),  # "frost", "konserves" or None
        "blend": bool(mixed(words, p)),  # 'Hakket oksekød m. 50% grønt'
    }
    # Leave out empty facts and flags that are false by default: 5 candidates x 50 items x
    # stores add up in tokens. size_fits stays when false: it differs from unknown.
    return {k: v for k, v in facts.items() if v is not None and not (k in USUALLY_FALSE and not v)}
