"""BilkaToGo recipes: real pages from bilkatogo.dk/inspiration/opskrifter (fixtures, 2026-11-06)."""

from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from handle_mcp.stores import StoreError
from handle_mcp.stores import bilkatogo_recipes as bilka_recipes
from handle_mcp.stores.bilkatogo import BilkaStore
from handle_mcp.stores.bilkatogo_recipes import category_path, js_object, recipe_path
from tests import test_bilkatogo

fake = test_bilkatogo.fake  # Bilka's login, cart and Algolia, fake
FIXTURES = Path(__file__).parent / "fixtures" / "bilkatogo" / "recipes"
BUILD = "1793920097"
PAGES = {  # payload path → fixture
    "": "opskrifter",
    "madplaner": "madplaner",
    "aftensmad": "aftensmad",
    "aftensmad/danske-retter": "danske-retter",
    "aftensmad/danske-retter/karbonader": "karbonader",
    "aftensmad/pastaretter/carbonara": "carbonara",
}
KARBONADER = "https://www.bilkatogo.dk/inspiration/opskrifter/aftensmad/danske-retter/karbonader/"


class FakeSite:
    """bilkatogo.dk: HTML with the build id, and each page's payload.js. A page it does not
    know answers like the real site: HTTP 200 with the website's HTML."""

    def __init__(self, bilka: test_bilkatogo.FakeBilka) -> None:
        self.bilka, self.build, self.paths = bilka, BUILD, []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "www.bilkatogo.dk":
            return self.bilka.handle(request)
        path = request.url.path
        self.paths.append(path)
        page = path.removeprefix(f"/_nuxt/static/{self.build}/inspiration/opskrifter")
        page = page.removesuffix("/payload.js").strip("/")
        if path.endswith("/payload.js") and path.startswith(f"/_nuxt/static/{self.build}/"):
            name = PAGES.get(page)
            if name is None and page.startswith("aftensmad/"):  # every other page: one of each
                name = "karbonader" if page.count("/") == 2 else "danske-retter"
            if name:
                return httpx.Response(200, text=(FIXTURES / f"{name}.payload.js").read_text())
        html = (FIXTURES / "unknown-page.html").read_text().replace(BUILD, self.build)
        return httpx.Response(200, text=html)


@pytest.fixture
def site(fake):
    return FakeSite(fake)


@pytest.fixture
def store(site):
    return BilkaStore(httpx.AsyncClient(transport=httpx.MockTransport(site.handle)))


def test_payload_is_read_as_json():
    data = js_object((FIXTURES / "karbonader.payload.js").read_text())
    page = data["data"][0]["page"]
    assert page["name"] == "karbonader" and page["hierarchy"] is None  # void 0 → null


@pytest.mark.parametrize(
    "given",
    [KARBONADER, KARBONADER + "?x=1", "/inspiration/opskrifter/aftensmad/danske-retter/karbonader"],
)
def test_recipe_path_from_link_or_path(given):
    assert recipe_path(given) == "aftensmad/danske-retter/karbonader"


@pytest.mark.parametrize("bad", ["../etc", "karbo nader", "", "https://www.bilkatogo.dk/"])
def test_recipe_path_must_be_a_path(bad):
    with pytest.raises(StoreError, match="Unknown"):
        recipe_path(bad)


def test_category_path_from_danish_words():
    assert category_path("Børn i køkkenet") == "boern-i-koekkenet"


async def test_recipe_has_text_and_products(store, fake):
    hits = test_bilkatogo.fixture("algolia_products")["hits"][:3]
    for hit, sku in zip(hits, ["81498", "125887", "137133"], strict=True):  # changed by hand
        hit["objectID"] = sku
    fake.scripted["/1/indexes/*/queries"] = [{"results": [{"hits": hits}]}]
    r = await store.recipe(KARBONADER, persons=6)
    assert (r.id, r.name, r.url) == ("aftensmad/danske-retter/karbonader", "Karbonader", KARBONADER)
    assert (r.persons, r.ingredients_persons, r.time) == (4, 4, "35 min")  # fixed, not 6
    assert r.description.startswith("Lav nemt klassiske karbonader")
    assert (
        r.image.startswith("https://digitalassets.sallinggroup.com/") and "jcr:26b30edc" in r.image
    )
    assert r.ingredients[:3] == ["500 g hakket svin", "1 æg", "1 løg"]
    assert "Karbonader" not in r.ingredients and not any("Forberedelse" in i for i in r.ingredients)
    assert len(r.method) == 1 and len(r.method[0].steps) == 11
    assert r.tip.startswith("Du kan prøve at lave vegetariske karbonader")
    assert [i.product.id for i in r.items] == ["81498", "125887", "137133"]  # the page's order
    assert all(i.product.quantity == 1 and not i.have_at_home for i in r.items)
    assert len(r.missing) == 12 and "17010" in r.missing  # 15 item numbers, 3 sold here
    assert r.total == sum((i.product.price for i in r.items), Decimal(0)) > 0
    sent = test_bilkatogo.json.loads(fake.requests[-1].content)["requests"][0]["params"]
    assert "objectID%3A81498+OR+objectID%3A125887" in sent and "17010" in sent


async def test_no_products_found_is_a_clear_error(store, fake):
    fake.scripted["/1/indexes/*/queries"] = [{"results": [{"hits": []}]}]
    with pytest.raises(StoreError, match="did not find any of the recipe products"):
        await store.recipe(KARBONADER)


async def test_only_item_numbers_go_in_the_filter(store, monkeypatch):
    asked = []

    async def algolia(query, limit, offers=False, ids=None):
        asked.extend(ids or [])
        return []

    monkeypatch.setattr(store, "_algolia", algolia)
    page = await store._page("aftensmad/danske-retter/karbonader")
    slider = next(n for n in bilka_recipes._nodes(page, "CuratedSlider"))
    slider["sourceProperties"]["curatedSkuList"] += ["1 OR isInOffer:1", None, 7]

    async def cached(_path):
        return page

    monkeypatch.setattr(store, "_page", cached)
    with pytest.raises(StoreError):
        await store.recipe(KARBONADER)
    assert len(asked) == 15 and all(a.isdigit() for a in asked)


async def test_method_and_tips_in_one_paragraph(store, fake):
    r = await store.recipe("aftensmad/pastaretter/carbonara")
    assert r.method[0].steps and r.tip and not r.tip.startswith(r.method[0].steps[-1])
    assert r.time == "20 min"


async def test_a_category_page_is_not_a_recipe(store):
    with pytest.raises(StoreError, match="is not a BilkaToGo recipe"):
        await store.recipe("aftensmad")


async def test_featured_is_this_weeks_madplan(store):
    week = await store.featured_recipes()
    assert [r.name for r in week][:2] == ["Cowboy toast", "Carbonara"] and len(week) == 7
    assert {r.group for r in week} == {"Madplan til uge 46"}
    assert week[1].id == "aftensmad/pastaretter/carbonara"


async def test_category_with_recipes(store):
    found = await store.recipes_by_tag([], "aftensmad/danske-retter", limit=25)
    assert len(found) == 15 and found[2].name == "Karbonader"
    assert {r.group for r in found} == {"Dansk inspirerede retter"}


async def test_category_with_sub_categories_mixes_them(store, site):
    found = await store.recipes_by_tag([], None, limit=5)  # aftensmad: 15 sub-categories
    assert len(found) == 5 and found[0].url.startswith("https://www.bilkatogo.dk/inspiration/")
    assert any(p.endswith("/aftensmad/suppe/payload.js") for p in site.paths)  # the last one


async def test_unknown_category_lists_the_real_ones(store):
    with pytest.raises(StoreError, match="does not recognize category 'pandekager'.*aftensmad, frokost"):
        await store.recipes_by_tag([], "pandekager")


async def test_tags_and_search_say_what_to_use(store):
    with pytest.raises(StoreError, match="does not support tags"):
        await store.recipes_by_tag(["Familievenlig"])
    with pytest.raises(StoreError, match="does not support recipe search"):
        await store.recipes("karry")


async def test_new_build_id_is_read_again(store, site):
    await store.featured_recipes()
    site.build = "1800000000"  # Bilka deployed
    await store.featured_recipes()
    assert site.paths.count("/inspiration/opskrifter/") == 2


async def test_no_build_id_is_a_clear_error(store, site):
    site.build = "nope"  # the HTML has no build id
    with pytest.raises(StoreError, match="format has changed: no build ID"):
        await store.featured_recipes()
