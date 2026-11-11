"""add_recipe_to_cart: a recipe's products into the cart with absolute counts, after a yes."""

from decimal import Decimal

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from handle_mcp import server
from handle_mcp.models import RecipeDetails, RecipeItem, RecipeProduct
from handle_mcp.recipe_cart import PlanError, plan
from tests.conftest import FakeStore


def _p(pid, name, qty=1, available=True):
    return RecipeProduct(pid, name, Decimal(10), qty, available=available)


KEBAB = RecipeDetails(
    "fake",
    "kebabfad-98001303",
    "Kebabfad",
    "https://example.com/kebabfad",
    6,
    items=[
        RecipeItem("Rødløg", _p("2301108", "Rødløg")),
        RecipeItem(
            "Hakket grise- og kalvekød",
            _p("5026536", "Hakket 8-12%", 2),
            alternatives=[_p("5062387", "Hakket 4-7%", 2)],
        ),
        RecipeItem("Agurk", _p("106593", "Agurk øko.", 2)),
        RecipeItem("Iceberg", _p("5004705", "Iceberg", 2, available=False)),
        RecipeItem("Salt", _p("5602504", "Salt"), have_at_home=True),
        RecipeItem("Olivenolie", _p("5049714", "Olivenolie"), have_at_home=True),
    ],
)


def test_plan_leaves_out_have_at_home_and_sold_out():
    lines, skipped = plan(KEBAB)
    assert {pid: ln["quantity"] for pid, ln in lines.items()} == {
        "2301108": 1,
        "5026536": 2,
        "106593": 2,
    }
    assert {(s["ingredient"], s["reason"]) for s in skipped} == {
        ("Iceberg", "sold out"),
        ("Salt", "assumed to be at home (the store assumes you have it)"),
        ("Olivenolie", "assumed to be at home (the store assumes you have it)"),
    }


def test_plan_skip_swap_and_include_by_name_or_id():
    lines, skipped = plan(
        KEBAB,
        skip=["agurk"],
        swap={"5026536": "5062387"},
        include=["Olivenolie"],
    )
    assert set(lines) == {"2301108", "5062387", "5049714"}
    assert lines["5062387"] == {
        "ingredients": ["Hakket grise- og kalvekød"],
        "name": "Hakket 4-7%",
        "quantity": 2,
    }
    assert ("Agurk", "skipped by the user") in {(s["ingredient"], s["reason"]) for s in skipped}


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"skip": ["Pizza"]}, "does not contain 'Pizza'.*Rødløg"),
        ({"swap": {"Rødløg": "999"}}, "is not an alternative for Rødløg"),
        ({"include": ["Peber"]}, "does not contain 'Peber'"),
    ],
)
def test_plan_unknown_names_are_clear_errors(kwargs, match):
    with pytest.raises(PlanError, match=match):
        plan(KEBAB, **kwargs)


@pytest.fixture
def kebab_store(monkeypatch):
    async def recipe(self, recipe_id, persons=None):
        return KEBAB

    monkeypatch.setattr(FakeStore, "recipe", recipe)


async def test_tool_adds_to_what_is_in_the_cart(kebab_store):
    await server.set_cart_quantities("fake", {"2301108": 3})  # onions from another recipe
    result = await server.add_recipe_to_cart("fake", "kebabfad-98001303", 6)
    onion = next(a for a in result["added"] if a["product_id"] == "2301108")
    assert (onion["before"], onion["added"], onion["after"], onion["ok"]) == (3, 1, 4, True)
    cart = {ln["product_id"]: ln["quantity"] for ln in result["cart"]["lines"]}
    assert {k: cart[k] for k in ("2301108", "5026536", "106593")} == {
        "2301108": 4,
        "5026536": 2,
        "106593": 2,
    }
    assert {s["ingredient"] for s in result["skipped"]} == {"Iceberg", "Salt", "Olivenolie"}


async def test_tool_twice_adds_twice(kebab_store):
    await server.add_recipe_to_cart("fake", "kebabfad-98001303")
    result = await server.add_recipe_to_cart("fake", "kebabfad-98001303")
    meat = next(a for a in result["added"] if a["product_id"] == "5026536")
    assert (meat["before"], meat["after"]) == (2, 4)


async def test_tool_bad_skip_writes_nothing(kebab_store):
    before = await server.get_cart("fake")
    with pytest.raises(ToolError, match="does not contain 'Pizza'"):
        await server.add_recipe_to_cart("fake", "kebabfad-98001303", skip=["Pizza"])
    assert await server.get_cart("fake") == before


async def test_tool_respects_the_quantity_limit(kebab_store):
    await server.set_cart_quantities("fake", {"5026536": server.MAX_QUANTITY})
    with pytest.raises(ToolError, match="At most"):
        await server.add_recipe_to_cart("fake", "kebabfad-98001303")


async def test_tool_is_a_write_that_is_not_idempotent():
    tool = next(t for t in await server.mcp.list_tools() if t.name == "add_recipe_to_cart")
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.idempotent_hint is False
    assert "ASK THE HUMAN FIRST" in tool.description and "twice adds twice" in tool.description


async def test_tool_clamps_persons(monkeypatch):
    seen = {}

    async def recipe(self, recipe_id, persons=None):
        seen["persons"] = persons
        return KEBAB

    monkeypatch.setattr(FakeStore, "recipe", recipe)
    await server.add_recipe_to_cart("fake", "kebabfad-98001303", 0)
    assert seen["persons"] == server.MIN_PERSONS
    await server.add_recipe_to_cart("fake", "kebabfad-98001303", 999)
    assert seen["persons"] == server.MAX_PERSONS


async def test_tool_reports_a_refused_line(kebab_store, monkeypatch):
    from handle_mcp.models import QuantityResult

    async def set_quantities(self, items):
        return [
            QuantityResult(p, q, 0 if p == "106593" else q, p != "106593") for p, q in items.items()
        ]

    monkeypatch.setattr(FakeStore, "set_quantities", set_quantities)
    result = await server.add_recipe_to_cart("fake", "kebabfad-98001303")
    ok = {a["product_id"]: a["ok"] for a in result["added"]}
    assert ok == {"2301108": True, "5026536": True, "106593": False}


def test_plan_swap_to_a_sold_out_alternative_is_skipped():
    sold_out = RecipeDetails(
        **{
            **KEBAB.__dict__,
            "items": [
                RecipeItem(
                    "Hakket", _p("1", "8-12%"), alternatives=[_p("2", "4-7%", available=False)]
                )
            ],
        }
    )
    lines, skipped = plan(sold_out, swap={"Hakket": "2"})
    assert not lines and skipped[0]["reason"] == "sold out"


async def test_tool_only_have_at_home_is_a_clear_error(monkeypatch):
    home = RecipeDetails(**{**KEBAB.__dict__, "items": KEBAB.items[-2:]})

    async def recipe(self, recipe_id, persons=None):
        return home

    monkeypatch.setattr(FakeStore, "recipe", recipe)
    with pytest.raises(ToolError, match="no products"):
        await server.add_recipe_to_cart("fake", "r")
