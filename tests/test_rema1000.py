"""REMA 1000 adapter against a fake backend (httpx.MockTransport). No network."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from handle_mcp import compare
from handle_mcp.stores import STORES, StoreError  # the real dict: conftest swaps in fakes later
from handle_mcp.stores.rema1000 import NOTE, Rema1000Store, product

FIXTURES = Path(__file__).parent / "fixtures" / "rema1000"
NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)  # inside the offer week in offers.json


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeRema:
    def __init__(self, answers=None) -> None:
        self.requests: list[httpx.Request] = []
        self.answers = answers or {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = request.url.params
        if params.get("filter[labels]") == "9001":
            return httpx.Response(200, json=fixture("offers"))
        key = params.get("query")
        if key in self.answers:
            return self.answers[key]
        return httpx.Response(200, json=fixture("search_milk"))


@pytest.fixture
def fake():
    return FakeRema()


@pytest.fixture
def store(fake):
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    return Rema1000Store(client=client)


def test_registered():
    assert STORES["rema1000"] is Rema1000Store


def test_product_fields():
    milk = product(fixture("search_milk")["data"][0], NOW)
    assert milk.store == "rema1000" and milk.id == "20302"
    assert milk.name == "SØDMÆLK 3,5%"  # as REMA writes it
    assert milk.price == Decimal("7.95") and milk.unit_price == Decimal("15.9")
    assert (milk.size, milk.brand, milk.unit) == ("0.5 LTR.", "ARLA", "l")
    assert milk.ean == "5700426101026"
    assert milk.category == "Mejeri > Mælk m.v."
    assert not milk.on_offer and milk.offer_text is None and milk.before_price is None


def test_offer_fields():
    bread = product(fixture("offers")["data"][0], NOW)
    assert bread.on_offer and bread.campaign == "Avisvare"
    assert bread.price == Decimal(10) and bread.before_price == Decimal("22.5")
    assert bread.offer_until == date(2026, 11, 14)
    assert bread.max_per_order == 6
    assert bread.offer_text == "Avisvare, maks 6 stk. (derefter 22,50 kr)"


def test_offer_ended_uses_the_next_period():
    bread = product(fixture("offers")["data"][0], datetime(2026, 11, 16, tzinfo=UTC))
    assert not bread.on_offer and bread.price == Decimal("22.5") and bread.offer_until is None


def test_no_price_is_skipped():
    assert product({"id": 1, "name": "X", "prices": []}) is None


async def test_search_sends_rema_headers(store, fake):
    hits = await store.search("mælk", limit=2)
    assert [p.name for p in hits] == ["SØDMÆLK 3,5%", "CHOKOLADEKIKS MÆLK"]
    req = fake.requests[0]
    assert req.url.host == "api.digital.rema1000.dk" and req.url.path == "/api/search/products"
    assert req.headers["X-Device"] == "web" and "authorization" not in req.headers
    assert req.method == "GET"


async def test_search_pages_until_limit(fake):
    page = fixture("search_milk")
    page["meta"]["pagination"] |= {"last_page": 2}
    fake.answers["x"] = httpx.Response(200, json=page)
    store = Rema1000Store(httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)))
    hits = await store.search("x", limit=5)
    assert len(hits) == 5 and [r.url.params["page"] for r in fake.requests] == ["1", "2"]


async def test_offers(store, fake):
    offers = await store.offers(limit=10)
    assert offers and all(p.on_offer for p in offers)
    assert fake.requests[0].url.params["sort"] == "department"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text="<html>Vent venligst</html>"),  # a queue or bot page
        httpx.Response(403, text="nej"),
        httpx.Response(200, json={"error": "x"}),
    ],
)
async def test_odd_answers_are_clear_errors(fake, answer):
    fake.answers["x"] = answer
    store = Rema1000Store(httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)))
    with pytest.raises(StoreError, match="REMA 1000"):
        await store.search("x")


async def test_pickup_only_terms(store):
    terms = await store.terms()
    assert not terms.home_delivery and terms.delivery_mode == "pickup"
    info = await store.delivery_info()
    assert info.note == NOTE and "packing fee" in NOTE


@pytest.mark.parametrize(
    "method,args",
    [("orders", ()), ("recipes", ("suppe",))],
)
async def test_not_yet(store, method, args):
    with pytest.raises(NotImplementedError):
        await getattr(store, method)(*args)


async def test_compare_says_pickup_and_missing_fee(fake):
    fake.answers["hakket oksekød"] = httpx.Response(200, json=fixture("search_oksekod"))
    store = Rema1000Store(httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)))
    quote = await compare.quote(store, ["hakket oksekød 400 g"])
    assert quote.notes == [NOTE]  # no "Pickup at None", no "not set to home delivery"
    assert quote.delivery_mode == "pickup" and quote.fees == {}
    # "HK." is hakket, and the 35 % grønt blend loses to plain minced beef.
    assert quote.lines[0].product.name == "HK. OKSEKØD 15-18%"


def test_verdict_says_cheapest_is_pickup():
    def quote(name, mode, total):
        return compare.BasketQuote(
            store=name,
            lines=[compare.BasketLine("mælk", 1, None, Decimal(total), True, None)],
            missing=[],
            products_total=Decimal(total),
            fees={},
            total=Decimal(total),
            minimum_order=None,
            minimum_met=None,
            notes=[],
            delivery_mode=mode,
            pickup_store=None,
        )

    result = compare.verdict(
        {"rema1000": quote("rema1000", "pickup", 8), "nemlig": quote("nemlig", "home", 9)}
    )
    assert result["cheapest"] == "rema1000" and "pickup" in result["summary"]


async def test_offers_skip_ended_offers_and_still_fill_the_limit(fake):
    page1, page2 = fixture("offers"), fixture("offers")
    for p in page1["data"]:  # the offer week is over: normal price only
        p["prices"] = [p["prices"][1]]
    page1["meta"]["pagination"] |= {"last_page": 2}
    page2["meta"]["pagination"] |= {"current_page": 2, "last_page": 2}
    pages = iter([page1, page2])

    def handle(request):
        fake.requests.append(request)
        return httpx.Response(200, json=next(pages))

    store = Rema1000Store(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    offers = await store.offers(limit=2)
    assert len(offers) == 2 and all(p.on_offer for p in offers)
    assert len(fake.requests) == 2


def test_comma_blend_only_for_rema():
    from handle_mcp.match import mixed

    rema = {"store": "rema1000", "name": "HK. OKSEKØD, 35% GRØNT"}
    nemlig = {"store": "nemlig", "name": "Skinke, 12% vand"}
    assert mixed(["hakket", "oksekød"], rema) == 1
    assert mixed(["skinke"], nemlig) == 0


def test_every_period_ended_never_shows_an_old_offer():
    late = datetime(2027, 6, 1, tzinfo=UTC)  # after both periods in offers.json
    bread = product(fixture("offers")["data"][0], late)
    assert bread is not None and not bread.on_offer and bread.price == Decimal("22.5")
    only_offer = fixture("offers")["data"][0] | {
        "prices": [fixture("offers")["data"][0]["prices"][0]]
    }
    assert product(only_offer, late) is None
