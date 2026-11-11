"""nemlig.com nutrition: the "Næringsindhold" table on the product page, from productbff.

GET productbff/api/web/productDetails/{id}: `product.detailDeclaration[]` has `tableHeader`
("Næringsindhold pr. 100 ml", "") and `tableItems` ([label, value] rows). Works with the
anonymous token (seen live 2026-11-11). One call per product, so search does not use it.
No EAN in it. Shapes: tests/fixtures/nemlig/README.md.
"""

from __future__ import annotations

import re
from typing import Any

from handle_mcp import nutrition
from handle_mcp.models import ProductNutrition
from handle_mcp.stores import StoreError
from handle_mcp.stores._http import per_product
from handle_mcp.stores.nemlig_recipes import BFF

NO_LABEL = "nemlig does not provide a nutrition label for this product (often the case for fresh meat, fruit and vegetables)."
UNKNOWN = "nemlig does not recognize this product."


def product_nutrition(product_id: str, data: dict) -> ProductNutrition:
    p = data["product"]
    label, per = None, None
    for block in p.get("detailDeclaration") or []:
        items = block.get("tableItems") or []
        rows = [(str(r[0] or ""), str(r[1] or "")) for r in items if len(r) >= 2]
        per = nutrition.basis(" ".join(str(h or "") for h in block.get("tableHeader") or []))
        if label := nutrition.label(rows, per):
            break
    return ProductNutrition(
        store="nemlig",
        id=str(p.get("id") or product_id),
        name=p.get("title") or None,
        nutrition=label,
        reason=None if label else NO_LABEL,
    )


class NutritionMixin:
    """nutrition() for NemligStore. Uses its `_public` (login optional)."""

    _public: Any

    async def nutrition(self, product_ids: list[str]) -> list[ProductNutrition]:
        if bad := [i for i in product_ids if not re.fullmatch(r"\d{1,12}", i)]:
            raise StoreError(
                f"Unknown product ID at nemlig: {', '.join(bad)}. Use the ID returned by product search."
            )

        async def one(pid: str) -> ProductNutrition:
            data = await self._public("GET", f"{BFF}/productDetails/{pid}")  # 404: NotFound
            try:
                return product_nutrition(pid, data)
            except (KeyError, TypeError, AttributeError) as exc:
                raise StoreError("nemlig nutrition data has changed format.") from exc

        return await per_product("nemlig", product_ids, one, UNKNOWN)
