"""nemlig recipes: real answers from nemlig's productbff API (fixtures, 2026-11-03)."""

from decimal import Decimal

import pytest

from handle_mcp import server
from handle_mcp.stores import StoreError
from handle_mcp.stores.nemlig_recipes import category_path, recipe_slug
from tests import test_nemlig

fake, store = test_nemlig.fake, test_nemlig.store  # the fixtures with nemlig's fake HTTP

KEBAB = "https://www.nemlig.com/maaltider/opskrift/kebabfad-med-fladbrod-98001303"


async def test_recipe_has_the_products_to_buy(store):
    r = await store.recipe(KEBAB, 4)
    assert (r.id, r.persons, r.min_persons, r.max_persons) == (
        "kebabfad-med-fladbrod-98001303",
        4,
        2,
        12,
    )
    assert (r.time, r.url, len(r.ingredients)) == ("40 min", KEBAB, 17)
    buy = [i for i in r.items if not i.have_at_home]
    home = [i.ingredient for i in r.items if i.have_at_home]
    assert len(buy) == 10
    assert home == ["Neutral olie", "Salt", "Peber", "Olivenolie", "Sukker"]
    meat = next(i for i in buy if i.ingredient == "Hakket grise- og kalvekød")
    assert (meat.product.id, meat.product.quantity, meat.product.price) == (
        "5026536",
        1,
        Decimal("29.95"),
    )
    assert meat.alternatives and meat.alternatives[0].id != meat.product.id
    assert r.total == Decimal("155.40")  # the 10 products, not the have-at-home ones


async def test_recipe_sends_slug_and_persons(store, fake):
    await store.recipe("/opskrifter/kebabfad-med-fladbrod-98001303", 6)
    dialog = next(r for r in fake.requests if r.url.path.endswith("/recipeProductDialog"))
    assert dialog.url.params["recipeId"] == "kebabfad-med-fladbrod-98001303"
    assert dialog.url.params["servings"] == "6"
    assert dialog.url.params["timeSlotId"] == "2435672"  # the basket's delivery time


@pytest.mark.parametrize("bad", ["kebab%2f..", "kebab fad", ""])
def test_recipe_id_must_be_a_slug(bad):
    with pytest.raises(StoreError, match="Unknown recipe"):
        recipe_slug(bad)


async def test_recipe_format_change_is_a_clear_error(store, monkeypatch):
    async def empty(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(store, "_api", empty)
    with pytest.raises(StoreError, match="has changed format"):
        await store.recipe(KEBAB)


async def test_featured_recipes_are_grouped(store):
    recipes = await store.featured_recipes(20)
    assert len(recipes) == 16
    assert recipes[1].name == "Kebabfad med fladbrød og hvidløgsdressing"
    assert recipes[1].group == "Mad i fad" and recipes[1].url == KEBAB
    assert len(await store.featured_recipes(3)) == 3


async def test_queued_recipe_api_is_a_clear_error(store, fake):
    fake.api_queue = True
    with pytest.raises(StoreError, match="queue"):
        await store.featured_recipes()


async def test_get_recipe_tool_clamps_persons(monkeypatch):
    seen = {}

    async def recipe(self, recipe_id, persons=None):
        seen["persons"] = persons
        return {"id": recipe_id}

    monkeypatch.setattr("tests.conftest.FakeStore.recipe", recipe)
    await server.get_recipe("fake", "r1", 99)
    assert seen["persons"] == server.MAX_PERSONS
    await server.get_recipe("fake", "r1", 1)
    assert seen["persons"] == server.MIN_PERSONS


async def test_recipe_has_photo_intro_and_method(store):
    r = await store.recipe(KEBAB, 4)
    assert r.image.startswith("https://media.umbraco.io/nemlig3/zu0cfnyn/kebabfad")
    assert r.description.startswith("Prøv denne lækre og nemme opskrift på kebabfad")
    assert [s.title for s in r.method] == ["Kebabfad", "Salat", "Dressing", "Servering"]
    assert [len(s.steps) for s in r.method] == [8, 5, 1, 2]
    assert r.method[0].steps[0] == "Tænd ovnen på 200 grader varmluft og sæt en bagerist i ovnen"
    assert r.tip is None  # nemlig sends "" when there is no tip


async def test_recipe_text_keeps_the_recipes_own_persons(store, monkeypatch):
    row = {"ingredientTitle": "Laks", "item": {"product": {"id": "1", "title": "Laks"}}}

    async def api(_method, url, **_kwargs):
        if url.endswith("/recipeProductDialog"):
            return {"includedProducts": [row], "servings": 6}
        return {"servings": 4, "ingredients": [{"ingredients": [{"text": "600 g laksefilet"}]}]}

    monkeypatch.setattr(store, "_api", api)
    r = await store.recipe(KEBAB, 6)
    assert (r.persons, r.ingredients_persons) == (6, 4)  # products for 6, text for 4
    assert r.ingredients == ["600 g laksefilet"]


async def test_recipe_with_odd_servings_or_images_does_not_crash(store, monkeypatch):
    row = {"ingredientTitle": "Laks", "item": {"product": {"id": "1", "title": "Laks"}}}

    async def api(_method, url, **_kwargs):
        if url.endswith("/recipeProductDialog"):
            return {"includedProducts": [row], "servings": "4-6"}
        return {
            "servings": "4-6",
            "images": [{"source": "http://x/plain.jpg"}, {"source": None}],
            "instructions": [{"title": None, "steps": [{"description": "  "}]}],
        }

    monkeypatch.setattr(store, "_api", api)
    r = await store.recipe(KEBAB, 4)
    assert (r.persons, r.ingredients_persons, r.image, r.method) == (4, None, None, [])


async def test_recipe_without_photo_or_method(store, monkeypatch):
    details = {
        "images": [{"source": "http://insecure.example/x.jpg"}, {"source": None}],
        "instructions": [
            {"title": "Tom", "steps": [{"description": "  "}, {}]},
            {"title": None, "steps": [{"description": " Rør rundt "}]},
        ],
        "tip": "  Server med brød  ",
    }
    row = {"ingredientTitle": "Løg", "item": {"product": {"id": "1", "title": "Løg"}}}

    async def api(_method, url, **_kwargs):
        return {"includedProducts": [row]} if url.endswith("/recipeProductDialog") else details

    monkeypatch.setattr(store, "_api", api)
    r = await store.recipe(KEBAB)
    assert (r.image, r.description, r.tip) == (None, None, "Server med brød")
    assert [(s.title, s.steps) for s in r.method] == [(None, ["Rør rundt"])]


async def test_recipe_without_price_or_servings(store, monkeypatch):
    row = {"ingredientTitle": "Løg", "item": {"product": {"id": "1", "title": "Løg"}}}

    async def api(_method, url, **_kwargs):
        return {"includedProducts": [row]} if url.endswith("/recipeProductDialog") else {}

    monkeypatch.setattr(store, "_api", api)
    r = await store.recipe(KEBAB, 6)
    assert (r.persons, r.items[0].product.price, r.total) == (6, Decimal(0), Decimal(0))


async def test_recipes_by_tag_title_or_path(store, fake):
    recipes = await store.recipes_by_tag(["Familievenlig", "budgetvenlig"], limit=5)
    assert len(recipes) == 5 and recipes[0].group == "Aftensmad"
    assert recipes[0].url.startswith("https://www.nemlig.com/maaltider/opskrift/")
    call = [r for r in fake.requests if r.url.path.endswith("/meals/allRecipes")][-1]
    assert call.url.params.get_list("filters") == ["familievenlig", "budgetvenlig"]
    assert "categoryPath" not in call.url.params  # nemlig's default: aftensmad


async def test_recipes_by_tag_in_a_category(store, fake):
    await store.recipes_by_tag(["Under 30 min"], category="Madpakke")
    call = [r for r in fake.requests if r.url.path.endswith("/meals/allRecipes")][-1]
    assert call.url.params["categoryPath"] == "madpakke"
    assert call.url.params.get_list("filters") == ["under-30"]


async def test_unknown_tag_lists_the_known_ones(store):
    with pytest.raises(StoreError, match="does not recognize tag 'Pizza'.*Familievenlig"):
        await store.recipes_by_tag(["Pizza"])


async def test_recipe_tags_are_fetched_once(store, fake):
    await store.recipes_by_tag(["Familievenlig"])
    await store.recipes_by_tag(["Kylling"])
    calls = [r for r in fake.requests if r.url.path.endswith("/meals/allRecipes")]
    assert len(calls) == 3  # one for the tag list, one per search


async def test_list_recipes_tool_picks_featured_or_tags():
    featured = await server.list_recipes(["fake"])
    tagged = await server.list_recipes(["fake"], tags=["Familievenlig"])
    assert featured["fake"][0]["group"] == "Morgen"
    assert tagged["fake"][0]["group"] == "Aftensmad"


async def test_recipe_search_gives_new_links(store, fake):
    recipes = await store.recipes("kylling karry", limit=3)
    assert [r.id for r in recipes] == [
        "nem-kylling-i-karry-98003871",
        "kylling-i-kokos-karry-98000533",
        "kylling-i-karry-98003870",
    ]
    assert (
        recipes[0].url == "https://www.nemlig.com/maaltider/opskrift/nem-kylling-i-karry-98003871"
    )
    assert recipes[0].name and recipes[0].time is None  # get_recipe has the time
    (call,) = [r for r in fake.requests if r.url.path.endswith("/search/recipes")]
    assert (call.url.params["query"], call.url.params["take"]) == ("kylling karry", "3")
    assert call.url.params["timeslotId"] == "2435672"
    assert "/searchgateway/api/search" not in fake.paths()


async def test_recipe_search_format_change_is_a_clear_error(store, monkeypatch):
    async def empty(*_args, **_kwargs):
        return {"searchResult": {}}

    monkeypatch.setattr(store, "_api", empty)
    monkeypatch.setattr(store, "_timeslot_id", _no_slot)
    with pytest.raises(StoreError, match="has changed format"):
        await store.recipes("karry")


async def _no_slot():
    return ""


def test_category_with_spaces_and_danish_letters():
    assert category_path("Mad til flere dage") == "mad-til-flere-dage"
    assert category_path("Måltidskasse") == "maaltidskasse"
    with pytest.raises(StoreError, match="Unknown category"):
        category_path("aftens/mad")


async def test_no_tags_from_nemlig_is_a_format_change(store, monkeypatch):
    async def api(*_args, **_kwargs):
        return {"filters": []}

    monkeypatch.setattr(store, "_api", api)
    with pytest.raises(StoreError, match="has changed format"):
        await store.recipes_by_tag(["Familievenlig"])
