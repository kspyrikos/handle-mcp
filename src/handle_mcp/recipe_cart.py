"""add_recipe_to_cart: which products a recipe adds to the cart, and the new absolute counts.

Pure functions: the tool in server.py fetches the recipe and the cart, and writes with the
store's set_quantities (absolute counts). The store's own "add recipe" call is never used.
"""

from __future__ import annotations

import dataclasses
from typing import Any


class PlanError(ValueError):
    """A skip/swap/include the recipe does not have. The text is for the human."""


def _dict(value: Any) -> dict:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return dict(value)


def _matches(key: str, item: dict) -> bool:
    key = key.strip().lower()
    return key in (str(item["ingredient"]).strip().lower(), str(item["product"]["id"]))


def plan(
    recipe: Any,
    skip: list[str] | None = None,
    swap: dict[str, str] | None = None,
    include: list[str] | None = None,
) -> tuple[dict[str, dict], list[dict]]:
    """→ (product id → {ingredients, name, quantity}, skipped items with a reason).

    have_at_home items (salt, oil) are skipped unless in `include`. `skip` and the keys of
    `swap`/`include` name an ingredient ("Hakket grise- og kalvekød") or a product id; a swap
    value must be one of that ingredient's alternatives. Unknown names raise PlanError."""
    items = [_dict(i) for i in _dict(recipe)["items"]]
    skip, swap, include = skip or [], swap or {}, include or []
    for key in [*skip, *swap, *include]:
        if not any(_matches(key, i) for i in items):
            names = ", ".join(i["ingredient"] for i in items)
            raise PlanError(f"The recipe does not contain {key!r}. Ingredients: {names}.")
    lines: dict[str, dict] = {}
    skipped = []
    for item in items:
        product = item["product"]
        reason = None
        if any(_matches(k, item) for k in skip):
            reason = "skipped by the user"
        elif item["have_at_home"] and not any(_matches(k, item) for k in include):
            reason = "assumed to be at home (the store assumes you have it)"
        if new_id := next((v for k, v in swap.items() if _matches(k, item)), None):
            product = next((a for a in item["alternatives"] if str(a["id"]) == str(new_id)), None)
            if product is None:
                ids = ", ".join(f"{a['id']} {a['name']}" for a in item["alternatives"]) or "none"
                raise PlanError(f"{new_id} is not an alternative for {item['ingredient']}: {ids}.")
        if reason is None and not product.get("available", True):
            reason = "sold out"
        if reason:
            skipped.append(
                {"ingredient": item["ingredient"], "name": product["name"], "reason": reason}
            )
            continue
        line = lines.setdefault(
            str(product["id"]), {"ingredients": [], "name": product["name"], "quantity": 0}
        )
        line["ingredients"].append(item["ingredient"])
        line["quantity"] += int(product["quantity"])
    return lines, skipped


def in_cart(cart: Any) -> dict[str, int]:
    """Product id → quantity in the cart now."""
    return {
        str(ln["product_id"]): int(ln["quantity"])
        for ln in (_dict(x) for x in _dict(cart).get("lines") or [])
    }


def report(lines: dict[str, dict], before: dict[str, int], results: list[Any]) -> list[dict]:
    """Before, added and after per product, from the store's set_quantities results."""
    by_id = {str(r["product_id"]): r for r in (_dict(x) for x in results)}
    return [
        {
            "product_id": pid,
            "name": ln["name"],
            "ingredients": ln["ingredients"],
            "before": before.get(pid, 0),
            "added": ln["quantity"],
            "after": by_id.get(pid, {}).get("applied"),
            "ok": by_id.get(pid, {}).get("ok", False),
            "message": by_id.get(pid, {}).get("message"),
        }
        for pid, ln in lines.items()
    ]
