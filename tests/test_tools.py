"""The tool surface: allowlist, write limits and error handling."""

import dataclasses
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from handle_mcp import server
from handle_mcp import stores as registry
from handle_mcp.models import Product
from handle_mcp.stores.bilkatogo import BilkaStore
from handle_mcp.stores.nemlig_offers import offer_product
from tests.conftest import BrokenStore

ALLOWED = {
    "search_products": False,
    "search_recipes": False,
    "get_recipe": False,
    "list_recipes": False,
    "list_offers": False,
    "compare_basket": False,
    "get_nutrition": False,
    "get_cart": False,
    "set_cart_quantities": True,
    "add_recipe_to_cart": True,
    "get_delivery_info": False,
    "list_delivery_slots": False,
    "get_orders": False,
    "get_order": False,
}


async def test_only_allowed_tools_exist():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert set(tools) == set(ALLOWED)


async def test_only_cart_tools_write():
    for tool in await server.mcp.list_tools():
        writes = ALLOWED[tool.name]
        assert tool.annotations.read_only_hint is (not writes), tool.name
        assert tool.annotations.destructive_hint is writes, tool.name


async def test_search_one_broken_store_does_not_stop_the_others():
    result = await server.search_products("mælk")
    assert result["fake"][0]["name"] == "mælk 1 l"
    assert result["fake"][0]["price"] == "12.95"
    assert "500" in result["broken"]["error"]


async def test_search_unknown_store_is_a_clear_error():
    with pytest.raises(ToolError, match="Unknown store 'netto'"):
        await server.search_products("mælk", stores=["netto"])


async def test_not_implemented_answers_politely():
    with pytest.raises(ToolError, match="does not support this yet"):
        await server.get_orders("broken")


async def test_set_cart_quantities_returns_results_and_new_cart():
    result = await server.set_cart_quantities("fake", {"1": 0, "2": 3})
    assert [r["applied"] for r in result["results"]] == [0, 3]
    assert [line["product_id"] for line in result["cart"]["lines"]] == ["2"]


@pytest.mark.parametrize(
    "items, match",
    [
        ({}, "No products"),
        ({"1": -1}, "Quantity must"),
        ({"1": 51}, "Quantity must"),
        ({str(i): 1 for i in range(51)}, "At most 50"),
    ],
)
async def test_set_cart_quantities_rejects_bad_input(items, match):
    with pytest.raises(ToolError, match=match):
        await server.set_cart_quantities("fake", items)


async def test_answers_are_json():
    result = await server.get_delivery_info("fake")
    json.dumps(result)
    assert result["delivery"]["chosen_start"] == "2026-11-03T16:00:00"
    assert result["terms"]["minimum_order"] == "500.00"


async def test_recipes_in_a_store_without_recipes_do_not_stop_the_others(monkeypatch):
    from tests.conftest import BrokenStore

    async def no_recipes(self, query, limit=5):
        raise NotImplementedError

    monkeypatch.setattr(BrokenStore, "recipes", no_recipes)
    result = await server.search_recipes("karry")
    assert result["fake"][0]["name"] == "karry til 4"
    assert result["broken"]["error"] == "broken does not support this yet."


async def test_delivery_slots_in_time_order_cheapest_first_only_free():
    fake = (await server.list_delivery_slots())["fake"]
    assert [s["price"] for s in fake["slots"]] == [
        "25.00",
        "19.00",
        "0.00",
    ]  # sold-out slot left out
    assert [s["price"] for s in fake["cheapest"]] == ["0.00", "19.00", "25.00"]
    assert fake["cheapest"][0]["start"] == "2026-11-04T14:00:00"


async def test_search_sort_puts_unknown_unit_price_last(monkeypatch):
    async def hits(self, query, limit=10):
        return [
            {"id": "a", "name": "A", "price": "30", "unit_price": None},
            {"id": "b", "name": "B", "price": "20", "unit_price": "80"},
            {"id": "c", "name": "C", "price": "10", "unit_price": "90"},
        ]

    monkeypatch.setattr("tests.conftest.FakeStore.search", hits)
    by_unit = await server.search_products("kylling", ["fake"], 3, sort="unit_price")
    by_price = await server.search_products("kylling", ["fake"], 3, sort="price")
    plain = await server.search_products("kylling", ["fake"], 3)
    assert [p["id"] for p in by_unit["fake"]] == ["b", "c", "a"]
    assert [p["id"] for p in by_price["fake"]] == ["c", "b", "a"]
    assert [p["id"] for p in plain["fake"]] == ["a", "b", "c"]  # the store's own order


async def test_search_by_price_puts_a_free_gift_first(monkeypatch):
    """On purpose: a free gift (price 0) is the cheapest. Its offer text says "max 1 stk."."""

    async def hits(self, query, limit=10):
        return [
            {"id": "bread", "name": "Rugbrød", "price": "25", "unit_price": "25"},
            {"id": "gift", "name": "Rosinboller", "price": "0", "unit_price": "0"},
        ]

    monkeypatch.setattr("tests.conftest.FakeStore.search", hits)
    result = await server.search_products("brød", ["fake"], 2, sort="price")
    assert [p["id"] for p in result["fake"]] == ["gift", "bread"]


async def test_get_cart_without_a_store_gives_every_cart(monkeypatch):
    from tests.conftest import BrokenStore

    async def no_login(_self):
        raise registry.StoreError("Intet login til broken.")

    monkeypatch.setattr(BrokenStore, "get_cart", no_login)
    carts = await server.get_cart()
    assert set(carts) == {"broken", "fake"}
    assert carts["fake"]["store"] == "fake" and "lines" in carts["fake"]
    assert "Intet login" in carts["broken"]["error"]  # one store failing never stops the other


async def test_get_cart_with_an_empty_store_name_is_an_error():
    with pytest.raises(ToolError, match="Unknown store ''"):
        await server.get_cart("")


async def test_get_cart_with_a_store_keeps_its_shape():
    cart = await server.get_cart("fake")
    assert cart["store"] == "fake" and "lines" in cart


def test_every_product_has_the_offer_fields_null_when_unknown():
    """docs/tools.md promises before_price and offer_until on every product, null when the
    store does not say. Every adapter builds a Product, so the dataclass is the contract."""
    fields = {f.name for f in dataclasses.fields(Product)}
    assert {"before_price", "offer_until"} <= fields
    product = asdict(Product(store="x", id="1", name="x", price=Decimal(1)))
    assert product["before_price"] is None and product["offer_until"] is None


def test_real_store_parsers_keep_the_offer_fields():
    """The nemlig and Bilka parsers go through Product too; this fails if one of them ever
    answers a plain dict. offers_page.json has a Skarp pris without a normal price."""
    page = json.loads(
        (Path(__file__).parent / "fixtures" / "nemlig" / "offers_page.json").read_text(
            encoding="utf-8"
        )
    )
    items = next(b["products"] for b in page["pageContent"] if (b.get("header") or {}).get("title"))
    checked = [p for raw in items if (p := offer_product(raw, "Skarp pris"))]
    assert checked and all(hasattr(p, "offer_until") for p in checked)
    assert any(p.before_price is None for p in checked)  # null, not a guess
    hits = json.loads(
        (Path(__file__).parent / "fixtures" / "bilkatogo" / "algolia_offers.json").read_text(
            encoding="utf-8"
        )
    )["hits"]
    bilka = [
        p for h in hits for sid in h.get("storeData") or {} if (p := BilkaStore()._product(h, sid))
    ]
    assert bilka and all(p.offer_until is None for p in bilka)  # Bilka never says


async def test_store_error_text_is_scrubbed(monkeypatch):
    """A store's own message reaches the model as ToolError text: it goes through scrub_text."""

    async def refuse(self, limit=10):
        raise registry.StoreError("Ordren til Kildevej 12, 2100 København er sendt til a@b.dk")

    monkeypatch.setattr(BrokenStore, "orders", refuse)
    with pytest.raises(ToolError) as err:
        await server.get_orders("broken")
    assert "Kildevej" not in str(err.value) and "a@b.dk" not in str(err.value)
    assert "broken:" in str(err.value)


async def test_store_error_in_a_many_store_answer_is_scrubbed(monkeypatch):
    async def refuse(self, query, limit=10):
        raise registry.StoreError("Kunden 98765432 findes ikke")

    monkeypatch.setattr(BrokenStore, "search", refuse)
    result = await server.search_products("mælk", stores=["broken"])
    assert "98765432" not in json.dumps(result)
