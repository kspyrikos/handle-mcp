"""The MCP server: every tool, store selection and error handling.

stdout is the JSON-RPC stream. All logging goes to stderr.
Every answer goes through privacy.scrub() in `_out()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BeforeValidator

from handle_mcp import cli, compare, links, recipe_cart, telemetry, translation
from handle_mcp import stores as registry
from handle_mcp.privacy import jsonable, scrub, scrub_text

log = logging.getLogger("handle_mcp")

MAX_ITEMS = 50  # per set_cart_quantities or compare_basket call
MAX_QUANTITY = 50  # per product
MAX_RESULTS = 25
MAX_DAYS = 14
MIN_PERSONS, MAX_PERSONS = 2, 12  # nemlig: 2-12


def _whole(value: Any) -> Any:
    """A count is a number or plain digits. int() reads "2_0" as 20 and True as 1."""
    if isinstance(value, bool) or (
        isinstance(value, str) and not re.fullmatch(r"\s*[0-9]+\s*", value)
    ):
        raise ValueError("Quantity must be a whole number, e.g. 2.")
    return value


Count = Annotated[int, BeforeValidator(_whole)]

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
)
ADD = ToolAnnotations(  # add_recipe_to_cart: twice adds twice
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)

mcp = MCPServer(
    "handle",
    instructions=(
        "Tools for Danish grocery stores with home delivery. Search products, compare baskets, "
        "find recipes, list delivery times and change cart quantities. You can never order or pay: "
        "the human always does that on the store's website. Store display text is translated from "
        "Danish to English locally by Argos Translate by default. Product IDs, EANs, URLs, prices, "
        "quantities, units and separate brand fields are preserved. An answer with "
        "`telemetry_notice` says what handle-mcp counts anonymously: tell the human in one "
        "sentence, once. If the human "
        "is new or unsure what to ask, suggest in their language: 'We have 4,000 kr. a month for "
        "food. Plan this week's meals.' Before a meal plan, if you do not know it yet, ask at most "
        "3 short questions: how many eat (adults and children), what they love and never eat, and "
        "which store (or the cheapest). Convert a monthly budget into a weekly one by dividing the remaining "
        "budget by the weeks left in the month, including this week, with a "
        "minimum of 1 week. Show that amount."
    ),
)

_instances: dict[str, registry.Store] = {}


def _store(name: str) -> registry.Store:
    if name not in registry.STORES:
        known = ", ".join(sorted(registry.STORES)) or "none yet"
        raise ToolError(f"Unknown store '{name}'. Known stores: {known}.")
    if name not in _instances:
        _instances[name] = registry.STORES[name]()
    return _instances[name]


def _names(requested: list[str] | None) -> list[str]:
    names = requested or sorted(registry.STORES)
    if not names:
        raise ToolError("No stores are configured yet.")
    for name in names:
        _store(name)  # fail early on an unknown name
    return names


def _persons(persons: int | None) -> int | None:
    return None if persons is None else max(MIN_PERSONS, min(persons, MAX_PERSONS))


def _tool(annotations: ToolAnnotations) -> Callable[[Callable], Callable]:
    """Register a tool, translate validation errors, and count the day's first tool call."""
    def decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def with_english_errors(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except ToolError as exc:
                message = await asyncio.to_thread(translation.translate_text, str(exc))
                raise ToolError(message) from exc

        return mcp.tool(annotations=annotations)(telemetry.counted(with_english_errors))

    return decorate


async def _out(value: Any) -> dict[str, Any]:
    # Scrub source text first so translated addresses cannot evade privacy rules.
    clean = scrub(jsonable(value))
    translated = await asyncio.to_thread(translation.translate_payload, clean)
    result = scrub(translated)
    result = result if isinstance(result, dict) else {"result": result}
    if notice := telemetry.notice():  # once: the model tells the human what is counted
        result["telemetry_notice"] = await asyncio.to_thread(translation.translate_text, notice)
    return result


def _error_text(store: str, exc: BaseException) -> str:
    """The error the model sees. A store's own message may echo personal data: scrub it."""
    return scrub_text(_error_reason(store, exc))


def _error_reason(store: str, exc: BaseException) -> str:
    if isinstance(exc, NotImplementedError):
        return f"{store} does not support this yet."
    if isinstance(exc, registry.StoreError):
        return f"{store}: {exc}"
    log.exception("%s failed", store, exc_info=exc)
    return f"{store}: unexpected error ({type(exc).__name__}). See the server log."


async def _call(store: str, method: str, *args: Any) -> Any:
    try:
        if method == "cart":  # the cart with its tagged link (links.py)
            return await links.cart(_store(store), *args)
        return await getattr(_store(store), method)(*args)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(_error_text(store, exc)) from exc


async def _each(names: list[str], work: Callable[[Any], Awaitable[Any]]) -> dict[str, Any]:
    """Run `work(store)` for every store at once. One store failing never stops the others."""
    results = await asyncio.gather(*(work(_store(n)) for n in names), return_exceptions=True)
    return {
        name: {"error": _error_text(name, r)} if isinstance(r, BaseException) else r
        for name, r in zip(names, results, strict=True)
    }


@_tool(READ)
async def search_products(
    query: str,
    stores: list[str] | None = None,
    limit: int = 10,
    sort: Literal["relevance", "price", "unit_price"] = "relevance",
) -> dict:
    """Search products in one or more stores. Returns price, price per kg/l, offers and EAN when
    known. Stores rank by relevance and put cheap products far down: when the human asks for the
    cheapest, use sort="unit_price" (per kg/l/stk) or "price"."""
    limit = max(1, min(limit, MAX_RESULTS))
    return await _out(await _each(_names(stores), lambda s: compare.search(s, query, limit, sort)))


@_tool(READ)
async def search_recipes(query: str, stores: list[str] | None = None, limit: int = 5) -> dict:
    """Find the stores' own recipes, e.g. "chicken curry". Returns each recipe's name and link.
    Use get_recipe for preparation time, serving count and shopping ingredients.
    nemlig only: BilkaToGo has no recipe search; use list_recipes with `category` instead."""
    limit = max(1, min(limit, MAX_RESULTS))
    return await _out(await _each(_names(stores), lambda s: s.recipes(query, limit)))


@_tool(READ)
async def get_recipe(store: str, recipe: str, persons: int | None = None) -> dict:
    """Return the shopping ingredients for one of the store's recipes. Each ingredient includes
    the store's product, pack count for `persons`, and alternatives. `have_at_home` items
    (such as salt and oil) are assumed to be available: they are excluded from `total`, so ask
    before adding them. Also returns `image`, `description`, `method` and `tip`. Scale
    `ingredients` using `ingredients_persons`. `recipe` is the recipe link or ID returned by
    search_recipes or list_recipes. Use add_recipe_to_cart only after the human approves.
    BilkaToGo: one pack per product, no alternatives; `persons` is fixed (null for piece counts).
    `missing` lists product IDs not sold at the delivery address."""
    return await _out(await _call(store, "recipe", recipe, _persons(persons)))


@_tool(READ)
async def list_recipes(
    stores: list[str] | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    limit: int = 20,
) -> dict:
    """Browse the stores' recipes and return their links. With no tags or category, return this
    week's featured recipes (nemlig's upstream label is "Ugens udvalgte"). With `tags`, return
    recipes matching every tag. The upstream nemlig tags are Danish labels such as "Familievenlig",
    "Budgetvenlig", "Under 30 min" and "Kylling"; pass those exact labels to the store.
    `category` is an upstream category path; nemlig defaults to "aftensmad". BilkaToGo does not
    support tags: without `category`, it returns the week's "Madplan" (7 dinners); with a category,
    it accepts paths such as "aftensmad/pastaretter". These Danish values are API labels, not
    translated display text. Use get_recipe for recipe details."""
    limit = max(1, min(limit, MAX_RESULTS))
    if not tags and not category:
        return await _out(await _each(_names(stores), lambda s: s.featured_recipes(limit)))
    return await _out(
        await _each(_names(stores), lambda s: s.recipes_by_tag(tags or [], category, limit))
    )


@_tool(READ)
async def list_offers(stores: list[str] | None = None, limit: int = 25) -> dict:
    """List this week's offers from each store, including products that ordinary product search
    might miss: nemlig's offer campaigns (upstream labels include "Skarp pris", "Priskup",
    "Dagstilbud" and "Undgå madspild"), BilkaToGo offers, and the REMA 1000 leaflet
    (upstream label "Avisvarer"). `limit` applies per list; duplicate products are returned once.
    A list that a store fails to provide is omitted (see the server log). Free gifts come from
    a separate search and are not limited by `limit`. They have price 0 and `max_per_order`."""
    limit = max(1, min(limit, MAX_RESULTS))
    return await _out(await _each(_names(stores), lambda s: s.offers(limit)))


@_tool(READ)
async def get_nutrition(store: str, product_ids: list[str]) -> dict:
    """Nutrition per 100 g or 100 ml from the store's label: energy_kj, energy_kcal, fat,
    saturated_fat, carbohydrate, sugar, protein, salt, fibre as strings ("0.13", "<0.5"), null
    when the label leaves a line out. No label (often fresh meat, fruit): `nutrition` null with a
    `reason`. Up to 50 ids from search_products, list_offers or compare_basket; duplicates are
    removed. nemlig, BilkaToGo and REMA 1000; no login needed."""
    ids = list(dict.fromkeys(i.strip() for i in product_ids if i.strip()))  # order kept
    if not ids or len(ids) > MAX_ITEMS:
        raise ToolError(
            "No product IDs provided." if not ids else f"At most {MAX_ITEMS} products per call."
        )
    return await _out({"products": await _call(store, "nutrition", ids)})


@_tool(READ)
async def compare_basket(items: list[str], stores: list[str] | None = None) -> dict:
    """Price a shopping list in each store, e.g. ["2 x whole milk 1 l", "rye bread", "eggs 10 pcs"].
    Specify the amount you need (for example "fior di latte 300 g"), not a package size. Returns
    a suggested product and up to five candidates per item, with the cost for the requested
    amount and flags such as `name_match`, `size_fits`, `prepared`, `processed`, `blend` and
    `oeko`. The suggested match is heuristic: choose the candidate that best fits the request,
    and ask the human when `sure` is false or no candidate is suitable. `search_products` can
    find more options. Unknown facts and false flags are omitted. Also returns the total,
    fees and whether the minimum order is met. This tool only reads; use set_cart_quantities
    with the chosen product IDs afterwards."""
    items = [i.strip() for i in items if i.strip()]
    if not items:
        raise ToolError("The shopping list is empty.")
    if len(items) > MAX_ITEMS:
        raise ToolError(f"At most {MAX_ITEMS} products per list.")
    quotes = await _each(_names(stores), lambda s: compare.quote(s, items))
    return await _out({"stores": quotes, **compare.verdict(quotes)})


@_tool(READ)
async def get_cart(store: str | None = None) -> dict:
    """Show the cart: lines, fees, total, whether the minimum order is met and `url`, the
    store's cart page where the human checks the cart and pays: give it to the human. With `store`
    that store's cart; without, every store's cart keyed by store name, a failing store
    (e.g. no login) as {"error": ...} next to the others. REMA 1000: the cart is the shopping
    list "handle-mcp" in REMA's app, where the human orders."""
    if store is not None:
        return await _out(await _call(store, "cart"))
    return await _out(await _each(_names(None), links.cart))


@_tool(WRITE)
async def set_cart_quantities(store: str, items: dict[str, Count]) -> dict:
    """Set ABSOLUTE quantities in one store's cart (0 removes the product). Never orders or pays. Returns a result per product and the new cart with its `url`: give the human that link to check and pay. REMA 1000: sets the shopping list "handle-mcp", never another list."""
    if not items:
        raise ToolError("No products provided.")
    if len(items) > MAX_ITEMS:
        raise ToolError(f"At most {MAX_ITEMS} products per call.")
    bad = [pid for pid, qty in items.items() if not 0 <= qty <= MAX_QUANTITY]
    if bad:
        raise ToolError(f"Quantity must be between 0 and {MAX_QUANTITY}. Invalid for: {', '.join(bad)}.")
    if telemetry.locked(store):  # was the counted cart ordered? (telemetry.cart_seen)
        with contextlib.suppress(ToolError):  # counting never blocks a cart change
            await _call(store, "cart")
    results = await _call(store, "set_quantities", dict(items))
    cart = await _call(store, "cart", False)
    telemetry.cart_filled(store, cart, results)
    return await _out({"results": results, "cart": cart})


@_tool(ADD)
async def add_recipe_to_cart(
    store: str,
    recipe: str,
    persons: int | None = None,
    skip: list[str] | None = None,
    swap: dict[str, str] | None = None,
    include: list[str] | None = None,
) -> dict:
    """Put a recipe's products in one store's cart, as get_recipe shows them.
    ASK THE HUMAN FIRST: show the list from get_recipe and get a yes. Never orders or pays.
    Adds to what is already in the cart, so calling it twice adds twice.
    `have_at_home` items (salt, oil) are left out unless named in `include`. `skip` leaves out
    ingredients; `swap` maps an ingredient (or product id) to one of its alternatives' product
    ids. Names: ingredient or product id. Returns before/added/after per product, the skipped
    items and the new cart. Check `ok` per product: a store can refuse some lines; tell the
    human which ones failed. BilkaToGo: one pack per product; `persons` is not used."""
    details = await _call(store, "recipe", recipe, _persons(persons))
    try:
        lines, skipped = recipe_cart.plan(details, skip, swap, include)
    except recipe_cart.PlanError as exc:
        raise ToolError(str(exc)) from exc
    if not lines:
        raise ToolError("There are no products to add to the cart.")
    if len(lines) > MAX_ITEMS:
        raise ToolError(f"At most {MAX_ITEMS} products per call.")
    before = recipe_cart.in_cart(await _call(store, "cart"))
    targets = {pid: before.get(pid, 0) + ln["quantity"] for pid, ln in lines.items()}
    if bad := [pid for pid, qty in targets.items() if qty > MAX_QUANTITY]:
        raise ToolError(f"At most {MAX_QUANTITY} units per product. Too many for: {', '.join(bad)}.")
    results = await _call(store, "set_quantities", targets)
    added = recipe_cart.report(lines, before, results)
    cart = await _call(store, "cart", False)
    telemetry.cart_filled(store, cart, results)
    return await _out({"added": added, "skipped": skipped, "cart": cart})


@_tool(READ)
async def get_delivery_info(store: str) -> dict:
    """Show the chosen delivery time, edit deadline and next free day, plus fees and minimum order.
    `delivery_mode` is "home", "pickup" (BilkaToGo, REMA 1000, with `pickup_store`) or null when
    unknown."""
    info, terms = await asyncio.gather(_call(store, "delivery_info"), _call(store, "terms"))
    return await _out({"delivery": info, "terms": terms})


@_tool(READ)
async def list_delivery_slots(stores: list[str] | None = None, days: int = 7) -> dict:
    """List available delivery or pickup slots for the next `days` days, sorted by time, plus
    the three cheapest slots per store. nemlig uses the upstream slot labels "Personlig" and
    "Fleksibel". BilkaToGo may return pickup slots when the cart is set
    to pickup. REMA 1000 returns Køb & Hent pickup slots for the store selected at login.
    These quoted Danish labels are retained because they come from the stores. This tool only
    reads availability; it never chooses a time, store or delivery mode. The human makes that
    choice on the store's website."""
    days = max(1, min(days, MAX_DAYS))
    return await _out(await _each(_names(stores), lambda s: _slots(s, days)))


async def _slots(store: registry.Store, days: int) -> dict:
    slots = sorted(
        (s for s in await store.delivery_slots(days) if s.available), key=lambda s: s.start
    )
    cheapest = sorted(slots, key=lambda s: (s.price is None, s.price or 0, s.start))[:3]
    return {"cheapest": cheapest, "slots": slots}


@_tool(READ)
async def get_orders(store: str, limit: int = 10) -> dict:
    """List recent orders in one store (newest first). Use get_order for the lines."""
    limit = max(1, min(limit, MAX_RESULTS))
    return await _out({"orders": await _call(store, "orders", limit)})


@_tool(READ)
async def get_order(store: str, order_id: str) -> dict:
    """Show one order with what was bought."""
    return await _out(await _call(store, "order", order_id))


def main() -> None:
    cli.main(mcp)


if __name__ == "__main__":
    main()
