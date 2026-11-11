"""BilkaToGo nutrition: the "Næringsindhold" table on the product page.

The page reads it from the public Algolia index: `infos` with code `nutritional_100`, rows
{type, title, value}, the first row "Næringsindhold pr." = "100 g". One request for every id,
and no address filter, so it needs no login (seen live 2026-11-11). Shapes:
tests/fixtures/bilkatogo/README.md.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlencode

from handle_mcp import nutrition
from handle_mcp.models import ProductNutrition
from handle_mcp.stores import StoreError
from handle_mcp.stores.bilkatogo_algolia import ALGOLIA, ALGOLIA_HEADERS, ALGOLIA_INDEX

NUTRITION_CODE = "nutritional_100"
NO_LABEL = "BilkaToGo does not provide a nutrition label for this product (often the case for fresh meat, fruit and vegetables)."
UNKNOWN = "BilkaToGo does not recognize this product."


def product_nutrition(hit: dict) -> ProductNutrition:
    info = next((i for i in hit.get("infos") or [] if i.get("code") == NUTRITION_CODE), None)
    rows = [(r.get("title") or "", r.get("value") or "") for r in (info or {}).get("items") or []]
    per = next((nutrition.basis(v) for t, v in rows if "pr." in t.lower()), None)
    label = nutrition.label(rows, per)
    return ProductNutrition(
        store="bilkatogo",
        id=str(hit["objectID"]),
        name=hit.get("name") or None,
        nutrition=label,
        reason=None if label else NO_LABEL,
    )


class BilkaNutritionMixin:
    """nutrition() for BilkaStore. Uses its `_send` and `_json`."""

    _send: Any
    _json: Any

    async def nutrition(self, product_ids: list[str]) -> list[ProductNutrition]:
        if bad := [i for i in product_ids if not re.fullmatch(r"\d{1,12}", i)]:
            raise StoreError(
                f"Unknown product ID at BilkaToGo: {', '.join(bad)}. Use the ID returned by product search."
            )
        params = urlencode(
            {
                "query": "",
                "hitsPerPage": len(product_ids),
                "filters": " OR ".join(f"objectID:{i}" for i in product_ids),
                "attributesToRetrieve": json.dumps(["name", "infos"]),
            }
        )
        body = {"requests": [{"indexName": ALGOLIA_INDEX, "params": params}]}
        resp = await self._send("POST", ALGOLIA, headers=ALGOLIA_HEADERS, json=body)
        if resp.status_code != 200:
            raise StoreError(f"BilkaToGo nutrition lookup failed ({resp.status_code}).")
        try:
            hits = {str(h["objectID"]): h for h in self._json(resp, "næring")["results"][0]["hits"]}
            found = {i: product_nutrition(h) for i, h in hits.items()}
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise StoreError("BilkaToGo nutrition data has changed format.") from exc
        return [
            found.get(i) or ProductNutrition("bilkatogo", i, None, None, UNKNOWN)
            for i in product_ids
        ]
