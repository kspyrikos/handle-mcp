"""compare_basket with a real week list against two fake stores. No network."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from handle_mcp import compare, match, server
from handle_mcp import stores as registry
from handle_mcp.candidates import brief
from handle_mcp.compare import best_match
from handle_mcp.match import is_oeko, pack_size, parse_item, parse_size, score
from handle_mcp.models import DeliveryInfo, Product, StoreTerms
from handle_mcp.privacy import jsonable
from handle_mcp.stores.bilkatogo import BilkaStore
from handle_mcp.stores.nemlig import NemligStore

WEEK_LIST = [
    "2 x letmælk 1 l",
    "rugbrød",
    "æg 10 stk",
    "smør 250 g",
    "hakket oksekød 500 g",
    "kyllingebryst",
    "bananer",
    "æbler",
    "kartofler 2 kg",
    "gulerødder 1 kg",
    "pasta 500 g",
    "havregryn 1 kg",
    "kaffe 400 g",
    "toiletpapir",
    "skyr 1 kg",
]


def p(store, pid, name, price, size=None, available=True, brand=None):
    return Product(store, pid, name, Decimal(price), brand, size, available=available)


NEMLIG = [
    p("nemlig", "n1", "Letmælk 0,5% fedt", "11.95", "1 l"),
    p("nemlig", "n2", "Kakaomælk", "9.95", "1 l"),
    p("nemlig", "n3", "Rugbrød m. kerner", "22.00", "1 kg", brand="Schulstad"),
    p("nemlig", "n4", "Økologiske æg", "34.95", "10 stk"),
    p("nemlig", "n5", "Smør", "24.95", "250 g", brand="Lurpak"),
    p("nemlig", "n6", "Hakket oksekød 8-12%", "45.00", "500 g"),
    p("nemlig", "n7", "Kyllingebryst", "59.00", "900 g"),
    p("nemlig", "n8", "Bananer", "3.00", "1 stk"),
    p("nemlig", "n9", "Æbler Pink Lady", "25.00", "1 kg"),
    p("nemlig", "n10", "Kartofler", "20.00", "2 kg"),
    p("nemlig", "n11", "Gulerødder", "12.00", "1 kg"),
    p("nemlig", "n12", "Pasta penne", "14.00", "500 g"),
    p("nemlig", "n13", "Havregryn", "16.00", "1 kg"),
    p("nemlig", "n14", "Kaffe formalet", "45.00", "500 g"),  # only 500 g
    p("nemlig", "n15", "Toiletpapir 8 ruller", "39.00", "8 items"),
    p("nemlig", "n16", "Skyr vanilje", "30.00", "1 kg"),
]
BILKA = [
    p("bilkatogo", "b1", "Letmælk", "10.50", "1 l"),
    p("bilkatogo", "b2", "Letmælk", "21.00", "2 l"),
    p("bilkatogo", "b3", "Rugbrød", "18.00", "1 kg"),
    p("bilkatogo", "b4", "Æg fra fritgående høns", "29.00", "10 stk"),
    p("bilkatogo", "b5", "Smør", "22.00", "250 g"),
    p("bilkatogo", "b6", "Hakket oksekød 8-12%", "40.00", "500 g"),
    p("bilkatogo", "b7", "Kyllingebrystfilet", "55.00", "900 g"),  # word inside a word
    p("bilkatogo", "b8", "Bananer", "2.50", "1 stk"),
    p("bilkatogo", "b9", "Æbler", "22.00", "1 kg"),
    p("bilkatogo", "b10", "Kartofler", "18.00", "2 kg"),
    p("bilkatogo", "b11", "Gulerødder", "10.00", "1 kg"),
    p("bilkatogo", "b12", "Pasta fusilli", "12.00", "500 g"),
    p("bilkatogo", "b13", "Havregryn", "15.00", "1 kg"),
    p("bilkatogo", "b14", "Kaffe", "42.00", "400 g"),
    p("bilkatogo", "b15", "Toiletpapir", "35.00", "8 items", available=False),  # sold out
    p("bilkatogo", "b16", "Skyr naturel", "28.00", "1 kg"),
]


class CatalogStore:
    """Searches a list like a store: any product whose name shares a word with the query."""

    name = "catalog"
    catalog: ClassVar[list[Product]] = []
    terms_ = StoreTerms("catalog", True)
    note: str | None = None

    def __init__(self) -> None:
        self.searches: list[str] = []

    async def search(self, query, limit=10):
        self.searches.append(query)
        words = [w for w in parse_item(query)[1] if len(w) > 1]
        found = [x for x in self.catalog if any(w[:5] in x.name.lower() for w in words)]
        return found[:limit]

    async def terms(self):
        return self.terms_

    async def delivery_info(self):
        return DeliveryInfo(self.terms_.store, note=self.note)


class Nemlig(CatalogStore):
    catalog = NEMLIG
    terms_ = StoreTerms("nemlig", True, Decimal(29), {"poser": Decimal(6)}, Decimal(300))
    note = "Leveringsgebyret afhænger af den valgte tid (19-51 kr.)."


class Bilka(CatalogStore):
    catalog = BILKA
    terms_ = StoreTerms("bilkatogo", True, Decimal(39), {"pakkeservice": Decimal(39)}, Decimal(500))


@pytest.fixture
def two_stores(monkeypatch):
    monkeypatch.setattr(registry, "STORES", {"nemlig": Nemlig, "bilkatogo": Bilka})


def test_parse_size_and_item():
    assert parse_size("1 l") == ("ml", Decimal(1000))
    assert parse_size("12 x 0,25 l / Classic") == ("ml", Decimal("3000.00"))
    assert parse_size("500 g") == ("g", Decimal(500))
    assert parse_size("Rugbrød") is None
    assert parse_item("2 x letmælk 1 l") == (2, ["letmælk"], ("ml", Decimal(1000)))
    assert parse_item("æg 10 stk") == (1, ["æg"], ("stk", Decimal(10)))
    assert parse_item("hakket oksekød med fedt") == (1, ["hakket", "oksekød", "fedt"], None)


@pytest.mark.parametrize(
    ("text", "size"),
    [
        ("1/2 dl olie", ("ml", Decimal(50))),  # was 2 dl: the number after the slash won
        ("½ dl kogevand", ("ml", Decimal(50))),  # was no size
        ("1½ dl fløde", ("ml", Decimal(150))),
        ("1 ½ dl fløde", ("ml", Decimal(150))),
        ("1 1/2 l mælk", ("ml", Decimal(1500))),
        ("3 3/4 dl fløde", ("ml", Decimal(375))),
        ("2/3 dl mel", ("ml", Decimal(200) / 3)),
        ("¼ kg smør", ("g", Decimal(250))),
        ("2 x 1/2 l", ("ml", Decimal(1000))),
        ("1/0 dl", None),
    ],
)
def test_parse_size_reads_fractions(text, size):
    assert parse_size(text) == size


def test_a_third_is_shown_with_two_decimals():
    assert jsonable(parse_size("2/3 dl mel")[1]) == "66.67"  # every tool answer goes via jsonable


def test_a_fraction_is_not_left_in_the_words():
    assert parse_item("1/2 dl olie til stegning") == (1, ["olie", "stegning"], ("ml", Decimal(50)))
    assert parse_item("2/3 dl mel")[1] == ["mel"]


def test_whole_word_beats_word_inside_word():
    line = best_match("mælk 1 l", [NEMLIG[1], p("x", "m", "Mælk", "12.00", "1 l")])
    assert line.product.id == "m" and line.sure  # not the cheaper Kakaomælk


def test_right_size_beats_cheaper_wrong_size():
    line = best_match("letmælk 2 l", BILKA[:2])
    assert line.product.id == "b2" and line.sure


def test_wrong_size_is_kept_but_marked():
    line = best_match("kaffe 400 g", NEMLIG)
    assert line.product.id == "n14" and not line.sure
    assert "Different package size" in line.note


def test_sold_out_is_missing():
    line = best_match("toiletpapir", [BILKA[14]])  # the only hit is sold out
    assert line.product is None and line.note == "Not found"


async def test_week_list_in_both_stores(two_stores):
    result = await server.compare_basket(WEEK_LIST)
    nemlig, bilka = result["stores"]["nemlig"], result["stores"]["bilkatogo"]

    assert nemlig["missing"] == []
    assert bilka["missing"] == ["toiletpapir"]
    milk = nemlig["lines"][0]
    assert (milk["product"]["id"], milk["quantity"], milk["line_total"]) == ("n1", 2, "23.90")
    assert all(ln["sure"] for ln in bilka["lines"])  # Kyllingebrystfilet is kyllingebryst
    assert [ln["item"] for ln in nemlig["lines"] if not ln["sure"]] == ["kaffe 400 g"]

    assert nemlig["fees"] == {"levering": "29.00", "poser": "6.00"}
    assert nemlig["products_total"] == "413.80"
    assert nemlig["total"] == "448.80"
    assert nemlig["minimum_met"] is True
    assert nemlig["notes"] == [Nemlig.note]
    assert bilka["minimum_met"] is False  # 334.50 kr. < 500 kr.

    # Compared on the 14 items both stores have, plus fees.
    assert result["comparable_totals"] == {"nemlig": "409.80", "bilkatogo": "412.50"}
    assert result["cheapest"] == "nemlig"  # Bilka is cheaper on goods but has higher fees
    assert "Check them" in result["summary"]


async def test_one_broken_store_does_not_stop_the_compare(monkeypatch):
    monkeypatch.setattr(
        registry, "STORES", {"nemlig": Nemlig, "bilkatogo": Bilka, "broken": BrokenCatalog}
    )
    result = await server.compare_basket(["rugbrød"])
    assert "500" in result["stores"]["broken"]["error"]
    assert result["cheapest"] == "nemlig"  # 22 + 35 kr. fees beats 18 + 78 kr.
    assert "Error: broken." in result["summary"]


async def test_no_winner_when_only_one_store_answers(monkeypatch):
    monkeypatch.setattr(registry, "STORES", {"nemlig": Nemlig, "broken": BrokenCatalog})
    result = await server.compare_basket(["rugbrød"])
    assert result["cheapest"] is None
    assert result["summary"] == "Only nemlig could price the list. Error: broken."


class BrokenCatalog(CatalogStore):
    async def search(self, query, limit=10):
        raise registry.StoreError("API'et svarer 500")


async def test_not_home_delivery_is_noted(monkeypatch):
    class Pickup(Bilka):
        terms_ = StoreTerms("bilkatogo", False)

    monkeypatch.setattr(registry, "STORES", {"bilkatogo": Pickup})
    result = await server.compare_basket(["rugbrød"])
    assert result["stores"]["bilkatogo"]["notes"] == ["The cart is not set to home delivery."]


async def test_pickup_is_noted(monkeypatch):
    class Pickup(Bilka):
        terms_ = StoreTerms("bilkatogo", False, delivery_mode="pickup", pickup_store="Bilka Vejle")

    monkeypatch.setattr(registry, "STORES", {"bilkatogo": Pickup})
    quote = (await server.compare_basket(["rugbrød"]))["stores"]["bilkatogo"]
    assert quote["notes"] == ["Pickup at Bilka Vejle: you collect the products yourself."]
    assert (quote["delivery_mode"], quote["pickup_store"]) == ("pickup", "Bilka Vejle")


async def test_pickup_without_store_adds_no_store_note(monkeypatch):
    """No "Afhentning i None", and no "ikke sat til hjemlevering" for a pickup store."""

    class Pickup(Bilka):
        terms_ = StoreTerms("bilkatogo", False, delivery_mode="pickup")

    monkeypatch.setattr(registry, "STORES", {"bilkatogo": Pickup})
    quote = (await server.compare_basket(["rugbrød"]))["stores"]["bilkatogo"]
    assert quote["notes"] == [] and quote["delivery_mode"] == "pickup"


async def test_empty_or_too_long_list_is_refused(two_stores):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="shopping list is empty"):
        await server.compare_basket([" ", ""])
    with pytest.raises(ToolError, match="At most"):
        await server.compare_basket(["mælk"] * (server.MAX_ITEMS + 1))


async def test_search_gets_words_only(two_stores):
    """Store search engines miss '2 x letmælk 1 l'; quantity and size are for matching only."""
    store = server._store("bilkatogo")
    await server.compare_basket(["2 x letmælk 1 l", "hakket oksekød 500 g", "æg 10 stk"])
    assert store.searches == ["letmælk", "hakket oksekød", "æg"]


async def test_compare_only_reads(two_stores):
    """compare_basket must never touch the cart: the fakes have no cart methods at all."""
    assert not hasattr(Nemlig, "set_quantities")
    await server.compare_basket(WEEK_LIST[:3])


def test_verdict_with_no_store():
    assert compare.verdict({"x": {"error": "nede"}})["cheapest"] is None


def test_verdict_without_common_items_names_no_winner():
    a = compare.BasketQuote(
        "a", [], ["x"], Decimal(0), {"levering": Decimal(19)}, Decimal(19), None, None
    )
    b = compare.BasketQuote(
        "b", [], ["x"], Decimal(0), {"levering": Decimal(39)}, Decimal(39), None, None
    )
    assert compare.verdict({"a": a, "b": b})["cheapest"] is None


async def test_failing_delivery_info_keeps_the_prices(monkeypatch):
    class NoDelivery(Nemlig):
        async def delivery_info(self):
            raise NotImplementedError

    monkeypatch.setattr(registry, "STORES", {"nemlig": NoDelivery})
    result = await server.compare_basket(["rugbrød"])
    assert result["stores"]["nemlig"]["lines"][0]["product"]["id"] == "n3"
    assert result["stores"]["nemlig"]["notes"] == [
        "Below the minimum order: 278.00 DKK remaining to reach 300 kr."
    ]


def test_singular_and_plural_match():
    line = best_match("bananer", [p("x", "b", "Banan øko.", "3.00", "1 stk")])
    assert line.product.id == "b" and line.sure  # singular and plural are the same product


def test_store_top_hit_when_no_name_matches():
    """Seen live: nemlig answers 'pasta' with 'Penne rigate'."""
    sold_out = p("x", "s", "Spaghetti", "9.00", "500 g", available=False)
    line = best_match("pasta 500 g", [sold_out, p("x", "pe", "Penne rigate", "12.00", "500 g")])
    assert line.product.id == "pe" and not line.sure
    assert "best search result" in line.note


def test_guesses_stay_out_of_the_verdict():
    good = compare.best_match("rugbrød", [p("a", "r", "Rugbrød", "20.00")])
    guess = compare.best_match("pasta", [p("b", "pe", "Penne", "1.00")])
    assert guess.guess
    quotes = {
        n: compare.BasketQuote(n, lines, [], Decimal(0), {}, Decimal(0), None, None)
        for n, lines in {"a": [good, good], "b": [good, guess]}.items()
    }
    quotes["a"].lines[1] = compare.BasketLine("pasta", 1, good.product, Decimal(50), True)
    verdict = compare.verdict(quotes)
    assert verdict["comparable_totals"] == {"a": Decimal(20), "b": Decimal(20)}  # pasta left out


def test_flavour_word_is_not_the_product():
    """Seen live: Bilka 'banan' matched 'Yoghurt m. banan' as sure."""
    yoghurt = p("x", "y", "Yoghurt m. banan", "12.00")
    line = best_match("14 x banan", [yoghurt, p("x", "b", "Bananer", "2.75", "1 stk")])
    assert line.product.id == "b"


def _cat(product, category):
    product.category = category
    return product


def test_m_without_dot_is_a_flavour():
    """Seen live 2 Nov: nemlig 'blåbær 250 g' picked 3 x 'Mini frugtstænger m blåbær øko'."""
    bar = _cat(p("x", "s", "Mini frugtstænger m blåbær øko", "9.45", "7 x 12,5 g"), "Kolonial")
    berries = _cat(p("x", "b", "Blåbær", "18.00", "125 g"), "Grønt")
    frozen = _cat(p("x", "f", "Blåbær", "29.50", "300 g / frost"), "Frost")
    assert best_match("blåbær 250 g", [bar, berries, frozen]).product.id == "b"


def test_cold_cuts_lose_to_raw_meat():
    """Seen live 2 Nov: Bilka 'kyllingebryst 450 g' picked 5 x 'Røget kyllingebryst' 100 g."""
    pålæg = "Mejeri & køl > Pålæg > Kødpålæg > Kyllingepålæg > Kyllingebryst i skiver"
    raw = "Kød & fisk > Kylling > Kyllingebryst & kyllingefilet > Kyllingebrystfilet"
    products = [
        _cat(p("x", "r", "Røget kyllingebryst", "11.00", "100 g"), pålæg),
        _cat(p("x", "s", "Kyllingebryst i skiver", "32.00", "350 g"), pålæg),
        _cat(p("x", "a", "Kyllingebrystfilet", "32.95", "280 g"), raw),
        _cat(p("x", "b", "Kyllingebrystfilet", "64.95", "650 g"), raw),
    ]
    assert best_match("kyllingebryst 450 g", products).product.id == "b"
    # Asked for: smoked still wins.
    assert best_match("røget kyllingebryst 100 g", products).product.id == "r"


def test_rolls_are_not_wet_wipes():
    """Seen live 2 Nov: Bilka 'toiletpapir 8 ruller' picked 42 wet wipes ('42 stk.')."""
    wipes = p("x", "w", "Aloe Vera fugtigt toiletpapir", "22.95", "42 stk.")
    rolls = p("x", "r", "Classic toiletpapir", "32.00", "12 rl.")
    assert best_match("toiletpapir 8 ruller", [wipes, rolls]).product.id == "r"
    nemlig = p("x", "n", "Toiletpapir 8 ruller", "39.00", "8 items")
    assert pack_size(nemlig) == ("rl", 8)
    # '8 stk' still finds rolls: only the other way round is closed.
    eight = p("x", "8", "Toiletpapir 3-lags", "28.00", "8 rl.")
    assert best_match("toiletpapir 8 stk", [eight]).sure


def test_rolls_count_as_pieces():
    """'6 x køkkenrulle' with no size: 6 rolls, so a 4-pack is bought twice, like '6 x bananer'."""
    four = p("x", "4", "Køkkenrulle 3-lags", "20.00", "4 rl.")
    one = p("x", "1", "Køkkenrulle 2-lags", "8.00", "1 rl.")
    line = best_match("6 x køkkenrulle", [four, one])
    assert (line.product.id, line.quantity, line.line_total) == ("4", 2, Decimal(40))


def test_only_cold_cuts_found_is_no_penalty():
    """Nothing raw in the search: the cheapest cold cut for the amount still wins."""
    pålæg = "Pålæg > Kyllingepålæg"
    smoked = _cat(p("x", "r", "Røget kyllingebryst", "11.00", "100 g"), pålæg)
    sliced = _cat(p("x", "s", "Kyllingebryst i skiver", "32.00", "350 g"), pålæg)
    assert best_match("kyllingebryst 400 g", [smoked, sliced]).product.id == "r"


def test_candidates_show_the_category():
    """So a human sees that the cheaper 'Cherrytomater' is a tin."""
    tin = _cat(p("x", "t", "Cherrytomater", "12.00", "400 g"), "Kolonial > Konserves")
    fresh = _cat(p("x", "f", "Cherry tomater øko", "17.00", "250 g"), "Frugt & grønt > Tomater")
    line = best_match("cherrytomater 250 g", [tin, fresh])
    assert line.product.id == "f"
    assert line.candidates[1:][0]["category"] == "Kolonial > Konserves"
    assert "brand" not in line.candidates[1]  # no brand: an empty fact is left out


def test_flavour_only_hit_is_never_a_guess():
    """Seen live: nemlig 'mandariner' matched 'Grøn te m. mandarin & appelsin'."""
    line = best_match("mandariner 1 kg", [p("x", "t", "Grøn te m. mandarin & appelsin", "31.50")])
    assert line.product is None and line.note == "Not found"


def test_store_category_breaks_a_tie_on_the_name():
    """Seen live: Bilka 'laks fersk' matched a nail polish."""
    polish = p("x", "n", "Neglelak laks", "29.00")
    polish.category = "Personlig pleje > Makeup > Neglelak"
    salmon = p("x", "l", "Laks", "85.00", "600 g")
    salmon.category = "Kød & fisk > Fisk > Laks"
    assert best_match("laks fersk", [polish, salmon]).product.id == "l"


def test_name_score_beats_the_category():
    coffee = p("x", "k", "Kaffe", "45.00")
    coffee.category = "Kolonial > Kaffe"
    filters = p("x", "f", "Kaffe filter", "20.00")
    filters.category = "Husholdning > Filtre"
    assert best_match("kaffe filter", [coffee, filters]).product.id == "f"


def test_partial_name_hit_is_still_a_guess():
    line = best_match("kaffe filter brun", [p("x", "f", "Kaffefilter", "20.00")])
    assert line.product.id == "f" and line.guess


def test_category_fits_a_compound_word():
    fresh = p("x", "f", "Cherrytomater", "15.00", "250 g")
    fresh.category = "Frugt & grønt > Grøntsager > Tomater"
    juice = p("x", "j", "Cherrytomater", "10.95", "400 g")
    juice.category = "Kolonial > Konserves > Dåsemad"
    assert best_match("cherrytomater", [juice, fresh]).product.id == "f"


def test_and_joins_two_products_not_a_flavour():
    line = best_match("peber", [p("x", "s", "Salt & peber", "15.00")])
    assert line.product.id == "s" and line.sure


def test_short_category_word_is_not_a_compound_hit():
    cheese = p("x", "o", "Post-it", "10.00")
    cheese.category = "Mejeri > Ost"
    assert match.category_rank(["post"], cheese) == 1


def test_relaxed_search_hit_is_marked_unsure():
    """Bilka retries with optional words: 'cheasy skyr passionsfrugt' can return another flavour."""
    other = p("x", "s", "Skyr m. skovbær 0,2%", "30.95", "1 kg", brand="Cheasy")
    line = best_match("4 x cheasy skyr passionsfrugt", [other])
    assert line.product.id == "s" and not line.sure and "Check the product" in line.note


def test_relaxed_search_hit_without_brand_is_only_a_guess():
    line = best_match("cheasy skyr passionsfrugt", [p("x", "n", "Skyr naturel", "20.00", "1 kg")])
    assert line.product.id == "n" and line.guess and not line.sure


def test_size_in_the_item_is_the_amount_wanted():
    line = best_match("hakket oksekød 1,5 kg", [p("x", "s", "Hakket oksekød", "35.00", "400 g")])
    assert (line.quantity, line.line_total, line.sure) == (4, Decimal("140.00"), True)


def test_cheapest_for_the_amount_wins():
    small = p("x", "s", "Laks", "30.00", "2 x 90 g")
    big = p("x", "b", "Laks", "89.00", "600 g")
    line = best_match("laks 600 g", [small, big])  # 4 small packs cost 120
    assert (line.product.id, line.quantity, line.line_total) == ("b", 1, Decimal("89.00"))


def test_quantity_without_size_picks_the_cheapest_line():
    bag = p("x", "bag", "Pærer", "22.00", "1 kg")
    one = p("x", "one", "Pære", "2.75", "1 stk")
    line = best_match("4 x pærer", [bag, one])
    assert (line.product.id, line.line_total) == ("one", Decimal("11.00"))


def test_cheapest_wins_on_the_same_name():
    eco = p("x", "eco", "Æg økologiske", "41.95", "10 stk")
    cheap = p("x", "cheap", "Æg frilands", "25.00", "10 stk")
    assert best_match("æg 10 stk", [eco, cheap]).product.id == "cheap"


def test_compound_word_counts_but_whole_word_wins():
    assert score(["mælk"], p("x", "k", "Kakaomælk", "8.00")) == 0.75
    whole = p("x", "m", "Mælk", "12.00", "1 l")
    cocoa = p("x", "k", "Kakaomælk", "8.00", "1 l")
    assert best_match("mælk 1 l", [cocoa, whole]).product.id == "m"


def test_much_bigger_pack_is_marked():
    line = best_match("kaffe 400 g", [p("x", "k", "Kaffe", "45.00", "500 g")])
    assert not line.sure and "Different package size" in line.note


def test_whole_word_beats_cheaper_compound_word():
    whole = p("x", "w", "Ris", "20.00", "1 kg")
    compound = p("x", "c", "Kokosris", "10.00", "1 kg")
    assert best_match("ris 1 kg", [compound, whole]).product.id == "w"


def test_how_it_is_made_is_the_same_product():
    eco = p("x", "eco", "Økologiske æg S", "13.95", "4 stk")
    offer = p("x", "offer", "Frilandsæg", "25.00", "10 stk")
    line = best_match("æg 10 stk", [eco, offer])  # 3 small packs would cost 41.85
    assert (line.product.id, line.line_total, line.sure) == ("offer", Decimal("25.00"), True)


def test_multi_buy_counts_in_the_line_total():
    legs = p("x", "l", "Kyllingelår", "113.50", "600 g")
    legs.offer_text = "2 for 175 kr"
    line = best_match("kyllingelår 1,2 kg", [legs])
    assert (line.quantity, line.line_total) == (2, Decimal(175))


def test_offer_that_ends_before_delivery_does_not_count():
    salmon = p("x", "s", "Laksefilet", "84.53", "600 g")
    salmon.before_price, salmon.offer_until = Decimal("120.75"), date(2026, 11, 4)
    line = best_match("laksefilet 600 g", [salmon], on=date(2026, 11, 9))
    assert line.line_total == Decimal("120.75") and "offer ends on 04/11" in line.note
    assert best_match("laksefilet 600 g", [salmon], on=date(2026, 11, 3)).line_total == Decimal(
        "84.53"
    )


def test_fresh_beats_cheaper_frozen_or_canned():
    frozen = p("x", "f", "Blåbær frost", "20.00", "300 g")
    fresh = p("x", "b", "Blåbær", "25.00", "125 g")
    assert best_match("blåbær", [frozen, fresh]).product.id == "b"
    canned = p("x", "c", "Cherrytomater i tomatjuice", "8.00", "400 g")
    canned.category = "Kolonial > Konserves"
    tomato = p("x", "t", "Cherrytomater", "15.00", "250 g")
    tomato.category = "Grønt"  # real products have a category: it says the item is produce
    assert best_match("cherrytomater", [canned, tomato]).product.id == "t"
    assert best_match("frosne blåbær", [frozen, fresh]).product.id == "f"


def test_frozen_meat_offer_still_wins():
    frozen = p("x", "f", "Hele kyllingelår m. rygben frost", "35.00", "1250 g")
    frozen.category = "Frost > Kød og fjerkræ"
    fresh = p("x", "k", "Hele kyllingelår", "64.95", "1.6 kg")
    assert best_match("kyllingelår 1,25 kg", [fresh, frozen]).product.id == "f"


def test_canned_by_name_or_category_loses():
    canned = p("x", "c", "Cherrytomater i tomatjuice", "10.95", "400 g")
    canned.category = "Kolonial > Konserves & dåsemad"
    fresh = p("x", "t", "Cherrytomater", "12.00", "400 g")
    fresh.category = "Frugt & grønt > Tomater"
    assert best_match("cherrytomater 250 g", [canned, fresh]).product.id == "t"


def test_canned_fish_is_not_demoted():
    # Real Bilka answer: canned 'Makrel i tomat' 125 g lost to 2 x 90 g spread from the fridge.
    line = best_match("makrel i tomat 125 g", _bilka("match_makrel_tomat"))
    assert (line.product.name, line.product.size, line.line_total, line.sure) == (
        "Makrel i tomat",
        "125 g",
        Decimal("13.95"),
        True,
    )


def test_fresh_broccoli_beats_frozen_and_one_piece():
    # Real nemlig answer: '2 x broccoli' took 2 x 1 stk (36 kr) over 2 x 500 g (30 kr).
    line = best_match("2 x broccoli", _nemlig("match_broccoli"))
    assert (line.product.size, line.line_total) == ("500 g", Decimal(30))


def test_cheapest_tin_beyond_the_top_ten():
    # Real nemlig answer: Taste of Nature (3 for 16 kr) is hit 14 of 30.
    products = _nemlig("match_hakkede_tomater_30")
    line = best_match("4 x hakkede tomater 400 g", products)
    assert line.product.size == "400 g / Taste of Nature"
    assert line.line_total == Decimal("22.37")  # 3 for 16 + 1 x 6.37
    assert compare.SEARCH_LIMIT >= 30


def test_mozzarella_is_only_a_guess_for_fior_di_latte():
    line = best_match("fior di latte 300 g", _bilka("match_mozzarella"))
    assert line.guess and not line.sure and "mozzarella" in line.product.name.lower()


def test_c_and_k_and_short_plural():
    assert best_match("klementiner", [p("x", "c", "Clementiner", "20.00")]).sure
    assert best_match("peberfrugt rød", [p("x", "p", "Peberfrugter røde", "20.00")]).sure


def test_canned_loses_even_when_fresh_has_no_fitting_category():
    """Seen live at Bilka: the canned one sits in '... > Cherrytomater', the fresh one does not."""
    canned = p("x", "c", "Cherrytomater i tomatjuice", "10.95", "400 g")
    canned.category = "Kolonial > Konserves, bouillon & krydderier > Tomatprodukter > Cherrytomater"
    fresh = p("x", "f", "Cherrytomater", "12.00", "400 g")
    fresh.category = "Frugt & grønt > Grønt"
    assert best_match("cherrytomater 250 g", [canned, fresh]).product.id == "f"


def test_split_compound_is_the_same_word():
    """Seen live at Bilka: every 'Cherrytomater' is canned; the fresh one is 'Cherry tomater øko'."""
    canned = p("x", "c", "Cherrytomater", "12.00", "400 g")
    canned.category = "Kolonial > Konserves > Flåede tomater"
    fresh = p("x", "f", "Cherry tomater øko", "17.00", "250 g")
    fresh.category = "Frugt & grønt > Grøntsager > Tomater > Cherrytomater"
    line = best_match("cherrytomater 250 g", [canned, fresh])
    assert line.product.id == "f" and line.sure


def test_same_cost_more_food_wins():
    small = p("x", "s", "Snackpeber mini", "22.00", "250 g")
    big = p("x", "b", "Snackpeber", "22.00", "500 g")
    assert best_match("snackpeber", [small, big]).product.id == "b"
    assert best_match("snackpeber", [big, small]).product.id == "b"


def test_delivery_day_is_danish():
    from datetime import UTC, datetime

    info = DeliveryInfo("x", chosen_start=datetime(2026, 11, 4, 23, 30, tzinfo=UTC))
    assert compare._day(info) == date(2026, 11, 5)  # 00:30 in Denmark


def test_blend_loses_to_plain_and_is_not_sure():
    """Seen live at nemlig: 'Hakket oksekød m. 50% grønt' was a sure match for minced beef."""
    blend = p("x", "g", "Hakket oksekød m. 50% grønt", "30.00", "500 g")
    plain = p("x", "o", "Hakket oksekød 14-18%", "47.44", "500 g")
    assert best_match("hakket oksekød 500 g", [blend, plain]).product.id == "o"
    only = best_match("hakket oksekød 500 g", [blend])
    assert not only.sure and "Mixed product" in only.note
    assert best_match("rugbrød", [p("x", "r", "Rugbrød m. kerner", "22.00", "1 kg")]).sure


def test_no_size_compares_per_kg_not_per_pack():
    """Seen live at Bilka: 'æbler' gave 1 apple for 2.75 while nemlig gave 1 kg."""
    one = p("x", "1", "Æbler", "2.75", "1 stk")
    bag = p("x", "b", "Æbler", "12.00", "1 kg")
    line = best_match("æbler", [one, bag])
    assert line.product.id == "b" and "Specify an amount" in line.note
    six = p("x", "6", "Toiletpapir", "20.00", "6 rl.")
    eight = p("x", "8", "Toiletpapir", "15.22", "8 rl.")
    assert best_match("toiletpapir", [six, eight]).product.id == "8"


def test_guess_with_a_size_is_the_cheapest_of_that_size():
    """Seen live at nemlig: 'pasta' only finds fresh pasta, and Tortelloni 250 g was the guess."""
    tortelloni = p("x", "t", "Tortelloni m. ricotta", "25.00", "250 g")
    fettuccine = p("x", "f", "Fettuccine", "13.95", "500 g")
    line = best_match("pasta 500 g", [tortelloni, fettuccine])
    assert (line.product.id, line.guess, line.sure) == ("f", True, False)
    assert best_match("pasta 500 g", [p("x", "s", "Penne", "9.00", "6 stk")]).product is None


def test_fat_share_or_size_is_not_a_blend():
    for name in ("Minimælk med 0,5% fedt", "Hakket svinekød u. 8% fedt", "Smør i 250 g"):
        assert match.mixed(["x"], p("x", "a", name, "10.00")) == 0
    assert match.mixed(["oksekød"], p("x", "b", "Oksekød m. 50% grønt", "10.00")) == 1


def test_count_without_size_prefers_pieces_and_unknown_size_comes_last():
    bag = p("x", "bag", "Pærer", "22.00", "1 kg")
    one = p("x", "one", "Pære", "2.75", "1 stk")
    loose = p("x", "loose", "Pærer", "1.00")  # no size at all
    assert best_match("4 x pærer", [loose, bag, one]).product.id == "one"
    assert best_match("pærer", [loose, one, bag]).product.id == "bag"


async def test_no_minimum_note_for_an_empty_basket(two_stores):
    result = await server.compare_basket(["findes ikke xyz"])
    assert not any("minimumskøb" in n for n in result["stores"]["nemlig"]["notes"])


def test_count_of_pieces_buys_enough_packs_not_n_packs():
    """Seen live at Bilka: '6 x bananer' gave 6 four-packs = 24 bananas."""
    four = p("x", "4", "Bananer", "10.00", "4 stk")
    line = best_match("6 x bananer", [four])
    assert (line.quantity, line.line_total) == (2, Decimal("20.00"))


def test_never_less_than_wanted():
    """Seen live: 3 x 300 g for 'hakket kylling 1 kg', 3 x 180 g for 'laks 600 g'."""
    small = p("x", "s", "Hakket kylling", "59.00", "300 g")
    assert best_match("hakket kylling 1 kg", [small]).quantity == 4
    assert best_match("mælk 1 l", [p("x", "m", "Mælk", "10.00", "990 ml")]).quantity == 1


def test_candidates_let_the_caller_swap():
    shallot = p("x", "s", "Skalotteløg banan", "15.00", "250 g")
    banana = p("x", "b", "Bananer", "3.00", "1 stk")
    cheap = p("x", "c", "Bananer øko", "2.50", "1 stk")
    line = best_match("bananer", [shallot, banana, cheap])
    ids = [a["id"] for a in line.candidates[1:]]
    assert line.product.id not in ids and len(ids) == 2
    assert {"id", "name", "size", "quantity", "line_total", "note"} <= set(line.candidates[1:][0])


def test_candidates_skip_store_guesses():
    banana = p("x", "b", "Bananer", "3.00", "1 stk")
    tea = p("x", "t", "Grøn te", "10.00", "100 g")
    assert best_match("bananer", [banana, tea]).candidates[1:] == []


def test_candidates_keep_the_note():
    small = p("x", "s", "Hakket kylling", "20.00", "300 g")
    big = p("x", "b", "Hakket kylling", "59.00", "1 kg")
    line = best_match("hakket kylling 1 kg", [small, big])
    assert line.product.id == "b"
    assert line.candidates[1:][0]["note"]


async def test_candidates_survive_out():
    banana = p("x", "b", "Bananer", "3.00", "1 stk")
    cheap = p("x", "c", "Bananer øko", "2.50", "1 stk")
    out = await server._out(best_match("bananer", [banana, cheap]))
    assert out["candidates"][1]["line_total"] in {"3.00", "2.50"}


def test_offer_limit_counts_extra_packs_at_the_normal_price():
    gift = p("x", "g", "Mums rosinboller", "0", "6 stk")
    gift.before_price, gift.max_per_order = Decimal("20.45"), 1
    assert best_match("rosinboller 6 stk", [gift]).line_total == Decimal(0)
    assert best_match("2 x rosinboller 6 stk", [gift]).line_total == Decimal("20.45")


def test_an_oeko_choice_is_always_shown():
    plain = [p("x", f"m{i}", "Fior di latte", f"{20 + i}.00", "200 g") for i in range(4)]
    eco = p("x", "eco", "Øko fior di latte", "40.00", "200 g")
    line = best_match("fior di latte 200 g", [*plain, eco])
    alts = line.candidates[1:]
    assert line.product.id == "m0" and len(alts) == 4  # 5 candidates with the suggestion
    assert alts[-1]["id"] == "eco" and alts[-1]["oeko"] is True
    assert "oeko" not in alts[0]


def test_no_extra_oeko_when_the_pick_is_oeko():
    eco = p("x", "eco", "Øko fior di latte", "10.00", "200 g")
    plain = [p("x", f"m{i}", "Fior di latte", f"{20 + i}.00", "200 g") for i in range(4)]
    line = best_match("fior di latte 200 g", [eco, *plain])
    assert line.product.id == "eco"
    assert [a["id"] for a in line.candidates[1:]] == ["m0", "m1", "m2", "m3"]


def test_multi_buy_counts_in_the_price_per_kg():
    """Week 45: 3 small øko packs on '3 for 48' beat a bigger pack that is cheaper per kg alone."""
    big = p("x", "big", "Fior di latte", "34.00", "200 g")  # 170 kr/kg
    eco = p("x", "eco", "Øko fior di latte", "22.00", "100 g")  # 220 kr/kg alone, 160 on offer
    eco.offer_text = "3 for 48 kr"
    line = best_match("3 x fior di latte", [big, eco])
    assert (line.product.id, line.quantity, line.line_total) == ("eco", 3, Decimal(48))


def test_multi_buy_for_the_amount_needed_and_a_hint():
    eco = p("x", "eco", "Øko fior di latte", "22.00", "100 g")
    eco.offer_text = "3 for 48 kr"
    line = best_match("fior di latte 200 g", [eco])
    assert (line.quantity, line.line_total) == (2, Decimal(44))  # never more than needed
    assert "1 more for 4.00 DKK" in line.note


def test_oeko_is_a_whole_word():

    assert is_oeko(p("x", "a", "Økologiske æg", "1"))
    assert is_oeko(p("x", "b", "Fior di latte øko", "1"))
    assert not is_oeko(p("x", "c", "Økonomipakke toiletpapir", "1"))


def test_offer_limit_caps_a_multi_buy():
    mix = p("x", "m", "Bananer", "2.75", "1 stk")
    mix.offer_text, mix.max_per_order = "Mix 10 for 25,00", 10
    assert best_match("10 x bananer", [mix]).line_total == Decimal(25)
    assert best_match("20 x bananer", [mix]).line_total == Decimal("52.50")  # 10 on offer


def test_pieces_pick_the_cheapest_for_the_count():
    """Live 2026-11-02, Bilka: '6 x bananer' took 2 x 4-pack (8 stk, 20 kr) on price per piece."""
    four = p("x", "four", "Bananer 4 pak øko", "10.00", "4 stk")
    one = p("x", "one", "Bananer", "2.75", "1 stk")
    six = p("x", "six", "Bananer 6 pak øko", "18.00", "6 stk")
    line = best_match("6 x bananer", [four, one, six])
    assert (line.product.id, line.quantity, line.line_total) == ("one", 6, Decimal("16.50"))
    assert "Ingen mængde" not in (line.note or "")


def test_pieces_say_when_packs_give_more():
    four = p("x", "four", "Bananer", "8.00", "4 stk")
    line = best_match("6 x bananer", [four])
    assert line.quantity == 2 and "8 items" in line.note


def test_candidates_have_a_price_and_lines_say_oeko():
    eco = p("x", "eco", "Øko fior di latte", "40.00", "200 g")
    plain = p("x", "m", "Fior di latte", "20.00", "200 g")
    line = best_match("fior di latte 200 g", [plain, eco])
    assert line.oeko is False and line.candidates[1:][0]["price"] == Decimal("40.00")


NEMLIG_FIX = Path(__file__).parent / "fixtures" / "nemlig"
BILKA_FIX = Path(__file__).parent / "fixtures" / "bilkatogo"


def _nemlig(name):
    store = NemligStore(httpx.AsyncClient())
    hits = json.loads((NEMLIG_FIX / f"{name}.json").read_text(encoding="utf-8"))["Products"][
        "Products"
    ]
    return [store._product(h) for h in hits]


def _bilka(name):
    store = BilkaStore(httpx.AsyncClient())
    hits = json.loads((BILKA_FIX / f"{name}.json").read_text(encoding="utf-8"))["hits"]
    return [x for h in hits if (x := store._product(h, "1653"))]


def test_fresh_garlic_beats_garlic_in_oil():
    # Real nemlig answer: "Hvidløg i krydderolie" was cheapest per kg and won.
    line = best_match("hvidløg", _nemlig("match_hvidloeg"))
    assert "olie" not in line.product.name
    assert line.product.category == "Grønt"


def test_garlic_with_an_amount_buys_one_bulb():
    for products in (_nemlig("match_hvidloeg"), _bilka("match_hvidloeg")):
        line = best_match("hvidløg 1 stk", products)
        assert line.sure and pack_size(line.product) == ("stk", 1)


def test_mackerel_in_tomato_finds_the_single_tin():
    # Real nemlig answer: the 4 x 125 g pack won, and no 125 g tin was even an alternative.
    line = best_match("makrel i tomat 125 g", _nemlig("match_makrel_tomat"))
    assert (line.product.name, line.line_total, line.sure) == (
        "Makrelstykker i tomat",
        Decimal("14.95"),
        True,
    )
    assert any(a["size"].startswith("125 g") for a in line.candidates[1:])


def test_preserved_is_a_whole_word_end():
    beer = p("x", "l", "Lagerøl", "8.00", "33 cl")
    oil = p("x", "o", "Hvidløg i krydderolie", "17.80", "290 g")
    assert match.processed_rank(["øl"], beer) == 0  # 'lage' inside a word is not 'lage'
    assert match.processed_rank(["hvidløg"], oil, fresh=True) == 1


async def test_lines_say_oeko_in_the_tool_answer(two_stores):
    result = await server.compare_basket(WEEK_LIST)
    lines = result["stores"]["nemlig"]["lines"]
    assert all(isinstance(ln["oeko"], bool) for ln in lines)
    assert all("price" in a for ln in lines for a in ln["candidates"])


def test_item_flavour_words_count_in_full():
    tin = p("x", "m", "Makrelfilet i tomat", "17.95", "125 g")
    assert score(["makrel", "tomat"], tin, {"tomat"}) == 1.0
    assert score(["makrel", "tomat"], tin) < 1.0  # 'tomat' alone is still a flavour


def test_brand_is_not_the_product():
    # Real Bilka answer: jam from "Fior di Frutta" matched "fior di latte".
    line = best_match("fior di latte", _bilka("match_fior_di_latte"))
    assert line.guess and not line.sure
    assert score(["fior", "di", "latte"], line.product) < 0.5
    assert "marmelade" not in line.product.name.lower()  # a brand-only hit is never the guess
    assert best_match("fior di latte 300 g", _bilka("match_fior_di_latte")).product is None


def test_two_broccoli_compares_a_pack_with_one_piece():
    # Live at nemlig: '2 x broccoli' took 2 x 1 stk at 36 kr, not 2 x 500 g at 30 kr.
    piece = p("x", "s", "Broccoli", "18.00", "1 stk.")
    pack = p("x", "g", "Broccoli", "15.00", "500 g")
    piece.category = pack.category = "Grønt"
    line = best_match("2 x broccoli", [piece, pack])
    assert (line.product.id, line.quantity, line.line_total) == ("g", 2, Decimal(30))
    assert "cheapest option is 2 packs" in line.note


def test_two_milk_still_compares_per_litre():
    small = p("x", "s", "Letmælk", "8.00", "500 ml")
    big = p("x", "b", "Letmælk", "12.00", "1 l")
    line = best_match("2 x letmælk", [small, big])
    assert line.product.id == "b" and "per kg, litre or item" in line.note


def test_without_a_fresh_hit_canned_is_not_demoted():
    oil = p("x", "o", "Hvidløg i krydderolie", "17.80", "290 g")
    paste = p("x", "p", "Hvidløgspasta", "25.00", "100 g")
    assert match.processed_rank(["hvidløg"], oil) == 0  # no 'Grønt' hit: not known produce
    assert best_match("hvidløg", [oil, paste]).product.id == "o"


def test_ready_meal_loses_to_a_raw_cut():
    """Seen live 2 Nov: Bilka 'laksefilet 600 g' picked 3 x 'Indbagt laksefilet m. spinat'.
    'Laksestykke' is laks like 'laksefilet': another cut of the same fish."""
    laks = "Kød & fisk > Fisk & skaldyr > Laks > Laksestykke"
    products = [
        _cat(p("x", "a", "Laksestykke", "109.95", "600 g", brand="Skagen food"), laks),
        _cat(p("x", "b", "Laksestykke", "59.00", "300 g", available=False), laks),
        _cat(p("x", "c", "Laksestykker", "49.95", "225 g"), laks),
        _cat(p("x", "r", "Røget laksestykke", "55.00", "200 g"), "Pålæg > Røget laks"),
        _cat(
            p("x", "i", "Indbagt laksefilet m. spinat", "19.00", "250 g"),
            "Frost > Kød & fisk > Fisk & skaldyr > Paneret & indbagt fisk > Indbagt laks",
        ),
    ]
    assert best_match("laksefilet 600 g", products).product.id == "a"


def test_baby_corn_is_not_corn():
    """Seen live 2 Nov: Bilka 'majs 340 g' picked 3 x 'Babymajs' 125 g (fresh) over tins."""
    tins = "Kolonial > Konserves, bouillon & krydderier > Konserveret frugt & grønt > Majs"
    products = [
        _cat(p("x", "1", "Majs", "12.25", "3 x 70 g"), tins),
        _cat(p("x", "2", "Majs", "7.95", "285 g"), tins),
        _cat(p("x", "3", "Majs", "8.00", "140 g"), tins),
        _cat(p("x", "f", "Søde majs", "13.27", "600 g"), "Frost > Grøntsager > Ærter & majs"),
        _cat(p("x", "b", "Babymajs", "6.50", "200 g"), tins),
        _cat(p("x", "g", "Babymajs", "15.00", "125 g"), "Frugt & grønt > Grøntsager > Minimajs"),
    ]
    assert "baby" not in best_match("majs 340 g", products).product.name.lower()


def test_thigh_cuts_are_kyllingelår():
    """Seen live 2 Nov: nemlig 'kyllingelår 1 kg' picked a 2 kg øko frost bag at 175 kr.
    'Kyllingeoverlår' and 'Kyllingeunderlår' are kyllingelår too, at a third of the price."""
    meat, frost = "Kød & fisk", "Frost"
    products = [
        _cat(p("x", "o", "Kyllingelår øko.", "97.95", "480 g / Kalu"), meat),
        _cat(p("x", "h", "Hele kyllingelår", "113.5", "600 g / Hopballe Mølle"), meat),
        _cat(p("x", "u", "Kyllingeunderlår", "39.95", "700 g / Rose"), meat),
        _cat(p("x", "r", "Kyllingeoverlår m. rygben", "29.95", "800 g / Rose"), meat),
        _cat(p("x", "z", "Hele kyllingelår u. ryg øko.", "175.0", "2,0 kg / frost"), frost),
    ]
    line = best_match("kyllingelår 1 kg", products)
    assert line.product.id in {"u", "r"} and line.line_total <= 80


def test_pickled_cucumbers_lose_to_fresh():
    """Seen live 2 Nov: nemlig 'agurk' picked 'Appetit agurker' (a jar in 'Kolonial')."""
    products = [
        _cat(p("x", "o", "Agurk øko.", "12.0", "1 stk."), "Grønt"),
        _cat(p("x", "d", "Agurk dansk", "20.0", "1 stk."), "Grønt"),
        _cat(p("x", "a", "Appetit agurker", "29.5", "570 g / 350 g / i skiver"), "Kolonial"),
        _cat(p("x", "s", "Agurkesalat", "16.95", "710 g / 405 g"), "Kolonial"),
    ]
    assert best_match("agurk", products).product.id == "o"


def test_half_a_name_on_a_fresh_vegetable_is_not_produce():
    """Seen live 2 Nov: 'Spaghettisquash' (Grønt) made dry spaghetti in 'Kolonial' lose to fresh."""
    dry = _cat(p("x", "d", "Spaghetti øko.", "7.95", "500 g / Casaneo"), "Kolonial")
    fresh = _cat(p("x", "f", "Spaghetti (frisk)", "20.95", "250 g / Rana"), "Køl")
    squash = _cat(p("x", "s", "Spaghettisquash øko.", "25.0", "1 stk."), "Grønt")
    assert best_match("spaghetti 500 g", [dry, fresh, squash]).product.id == "d"


def test_kolonial_dry_goods_win_when_nothing_fresh():
    """nemlig 'Kolonial' is the dry aisle. It loses only when the search found the item fresh."""
    dry = _cat(p("x", "d", "Røde linser", "14.00", "500 g"), "Kolonial")
    tin = _cat(p("x", "t", "Røde linser i dåse", "12.00", "400 g"), "Kolonial")
    assert best_match("røde linser 500 g", [dry, tin]).product.id == "d"
    pasta = _cat(p("x", "s", "Spaghetti", "7.95", "500 g"), "Kolonial")
    squash = _cat(p("x", "q", "Spaghettisquash", "15.00", "1 stk"), "Grønt")
    assert best_match("spaghetti 500 g", [pasta, squash]).product.id == "s"


def test_dried_herb_loses_to_fresh():
    """With a fresh full-name hit the item is produce: a dried 'Kolonial' jar loses."""
    dried = _cat(p("x", "d", "Basilikum tørret", "12.00", "10 g"), "Kolonial")
    fresh = _cat(p("x", "f", "Basilikum", "15.00", "1 stk"), "Grønt")
    assert best_match("basilikum", [dried, fresh]).product.id == "f"


def test_over_under_only_before_a_limb():
    """'Kyllingeoverlår' is kyllingelår. 'over' inside other words is not a part."""
    assert score(["kyllingelår"], p("x", "o", "Kyllingeoverlår", "60.00")) == 1
    assert score(["kartofler"], p("x", "k", "Kartoverofler", "10.00")) < 1


def test_candidates_put_the_suggestion_first_with_flags():
    """The caller picks: up to 5 candidates, the suggested one first, each with plain facts."""
    raw = "Kød & fisk > Kylling"
    products = [
        _cat(p("x", "r", "Røget kyllingebryst", "11.00", "100 g"), "Pålæg > Kyllingepålæg"),
        _cat(p("x", "b", "Kyllingebrystfilet", "64.95", "650 g"), raw),
        _cat(p("x", "a", "Kyllingebrystfilet", "32.95", "280 g"), raw),
        _cat(p("x", "f", "Kyllingebrystfilet", "239.00", "4 kg"), "Frost > Kylling"),
        _cat(p("x", "o", "Kyllingebrystfilet øko", "69.00", "225 g"), raw),
        _cat(p("x", "s", "Kyllingebryst i skiver", "32.00", "350 g"), "Pålæg > Kyllingepålæg"),
        _cat(p("x", "t", "Kyllingebrystfilet", "115.00", "1.2 kg"), raw),
    ]
    cands = best_match("kyllingebryst 450 g", products).candidates
    assert len(cands) == 5 and cands[0]["id"] == "b" and cands[0]["suggested"]
    assert not any(c.get("suggested") for c in cands[1:])
    assert any(c.get("oeko") for c in cands)  # øko is always among them
    by_id = {c["id"]: c for c in cands}
    assert by_id["b"]["name_match"] == "full"
    assert by_id["b"]["size_fits"] is False  # 650 g for 450 g: 'Anden størrelse'
    assert "prepared" not in by_id["b"] and "processed" not in by_id["b"]
    smoked = brief("kyllingebryst 450 g", best_match("røget kyllingebryst", products[:1]))
    assert smoked["prepared"] is True
    frozen = brief("kyllingebryst 450 g", best_match("kyllingebryst", [products[3]]))
    assert frozen["processed"] == "frost" and frozen["size_fits"] is False  # 4 kg for 450 g


def test_candidate_flags_canned_blend_and_no_size():
    tin = _cat(
        p("x", "t", "Cherrytomater", "12.00", "400 g"), "Kolonial > Konserves > Cherrytomater"
    )
    jar = _cat(p("x", "j", "Spaghetti", "7.95", "500 g"), "Kolonial")  # nemlig dry goods
    blend = p("x", "m", "Hakket oksekød m. 50% grønt", "30.00", "500 g")
    assert brief("cherrytomater", best_match("cherrytomater", [tin]))["processed"] == "konserves"
    plain = brief("spaghetti", best_match("spaghetti", [jar]))
    assert "processed" not in plain and "size_fits" not in plain  # unknown: left out
    assert brief("hakket oksekød", best_match("hakket oksekød", [blend]))["blend"] is True


def test_size_fits_is_unknown_when_sizes_cannot_be_compared():
    loose = p("x", "l", "Kyllingebryst", "40.00", None)
    two = p("x", "s", "Kyllingebryst", "40.00", "2 stk")
    assert "size_fits" not in brief("kyllingebryst 500 g", best_match("kyllingebryst", [loose]))
    assert "size_fits" not in brief("kyllingebryst 500 g", best_match("kyllingebryst", [two]))


async def test_six_pieces_fit_in_the_tool_answer():
    banana = p("x", "b", "Bananer", "3.00", "1 stk")
    out = await server._out(best_match("6 x bananer", [banana]))
    assert out["candidates"][0]["line_total"] == "18.00" and out["candidates"][0]["size_fits"]


def test_no_candidates_when_nothing_is_found():
    assert best_match("bananer 1 kg", []).candidates == []


def test_a_guess_is_still_the_suggestion_marked_no_name_match():
    # Intended: the caller sees the store's top hit, flagged, and can search again.
    line = best_match("pasta", [p("x", "pe", "Penne", "10.00", "500 g")])
    assert line.guess and len(line.candidates) == 1
    assert line.candidates[0]["suggested"] and line.candidates[0]["name_match"] == "none"
