"""nemlig.com adapter against a fake backend (httpx.MockTransport). No network."""

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from handle_mcp import compare, server
from handle_mcp import stores as registry
from handle_mcp.stores import StoreError, _http, nemlig_delivery, nemlig_offers
from handle_mcp.stores.nemlig import NemligStore
from handle_mcp.stores.nemlig_offers import offer_product

FIXTURES = Path(__file__).parent / "fixtures" / "nemlig"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeNemlig:
    """Answers like nemlig.com. Records every request. Tests can script failures."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.basket = fixture("basket") | {
            "TimeslotUtc": "2026110316-60-180",
            "DeliveryZoneId": 3,
            "DeliveryTimeSlot": {"Id": "2435672"},  # live basket field, 2026-11-03
        }
        self.scripted: dict[str, list] = {}  # path → status codes or errors to give first
        self.token_count = 0
        self.queue = False  # page URLs (/, /tilbud) go to queue-it, as seen live 2026-11-03
        self.api_queue = False  # the API too: not seen, but nemlig could
        self.endless_days = False
        self.history, self.details = "order_history", "order_details"

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.scripted.get(path):
            nxt = self.scripted[path].pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return httpx.Response(nxt)
        if request.url.host == "nemlig.queue-it.net":
            return httpx.Response(200, text="<html>Du er i kø</html>")
        if path in ("/", "/tilbud") and self.queue:  # page URLs, never /webapi/
            return httpx.Response(
                302, headers={"Location": "https://nemlig.queue-it.net/?c=nemlig"}
            )
        if path == "/webapi/AntiForgery":
            return httpx.Response(200, json={"Header": "X-XSRF-TOKEN", "Value": "xsrf-1"})
        if path == "/webapi/Token":
            self.token_count += 1
            return httpx.Response(
                200, json={"access_token": f"bearer-{self.token_count}", "expires_in": 300}
            )
        if path == "/webapi/login":
            body = json.loads(request.content)
            if body["Password"] != "hemmeligt":
                return httpx.Response(401)
            return httpx.Response(
                200,
                json={"RedirectUrl": "/", "TimeslotUtc": "x"},
                headers={"set-cookie": ".ASPXAUTH=auth-1; Path=/; HttpOnly"},
            )
        if path == "/webapi/v2/AppSettings/Website":
            return httpx.Response(200, json={"CombinedProductsAndSitecoreTimestamp": "ts-1"})
        if path.startswith("/productbff/") and self.api_queue:
            return httpx.Response(302, headers={"Location": "https://nemlig.queue-it.net/"})
        if path.startswith("/productbff/api/web/meals/recipeDetails/"):
            return httpx.Response(200, json=fixture("recipe_details"))
        if path == "/productbff/api/web/meals/recipeProductDialog":
            return httpx.Response(200, json=fixture("recipe_products"))
        if path == "/productbff/api/web/search/recipes":
            return httpx.Response(200, json=fixture("recipe_search"))
        if path == "/productbff/api/web/meals/allRecipes":
            return httpx.Response(200, json=fixture("recipes_by_tag"))
        if path == "/productbff/api/web/page":
            page = {"/maaltider": "meals_page", "/tilbud": "offers_page"}[
                request.url.params["path"]
            ]
            return httpx.Response(200, json=fixture(page))
        if path == "/searchgateway/api/search":
            if request.url.params.get("query") in ("mums rosinboller", "gratis"):
                return httpx.Response(200, json=fixture("search_gift"))
            if request.url.params.get("query") == "kyllingebryst":
                hits = fixture("search_kyllingebryst")  # live 2026-11-03: the cheapest is hit 30
                take = int(request.url.params["take"])
                hits["Products"]["Products"] = hits["Products"]["Products"][:take]
                return httpx.Response(200, json=hits)
            return httpx.Response(200, json=fixture("search"))
        if path == "/webapi/basket/GetBasket":
            if ".ASPXAUTH=auth-1" not in request.headers.get("cookie", ""):
                return httpx.Response(200, json={**self.basket, "Email": None})  # anonymous
            return httpx.Response(200, json=self.basket)
        if path == "/webapi/basket/AddToBasket":
            body = json.loads(request.content)
            pid, qty = body["ProductId"], body["quantity"]
            if pid == "sold-out":
                qty = 0  # nemlig quietly refuses
            lines = [ln for ln in self.basket["Lines"] if ln["Id"] != pid]
            if qty:
                lines.append({"Id": pid, "Name": "Vare", "Quantity": qty, "Price": 10.0 * qty})
            self.basket = {**self.basket, "Lines": lines}
            return httpx.Response(200, json=self.basket)
        if path == "/webapi/v2/Delivery/GetDeliveryDays":
            if request.url.params.get("startDate") and not self.endless_days:  # no more days
                return httpx.Response(200, json={"DayRangeHours": [], "NextRangeStart": None})
            return httpx.Response(200, json=fixture("delivery_days"))
        if path == "/webapi/Order/DeliverySpot":
            return httpx.Response(200, json=getattr(self, "spot", None) or fixture("delivery_spot"))
        if path == "/webapi/order/GetBasicOrderHistory":
            return httpx.Response(200, json=fixture(self.history))
        if path.startswith("/webapi/v2/order/GetOrderHistory/"):
            return httpx.Response(200, json=fixture(self.details))
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("HANDLE_NEMLIG_EMAIL", "kunde@example.com")
    monkeypatch.setenv("HANDLE_NEMLIG_PASSWORD", "hemmeligt")
    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    return FakeNemlig()


async def _no_sleep(_seconds):
    return None


@pytest.fixture
def store(fake):
    return NemligStore(
        httpx.AsyncClient(transport=httpx.MockTransport(fake.handle), follow_redirects=True)
    )


async def test_login_flow_then_authorized_calls(store, fake):
    await store.get_cart()
    assert fake.paths()[:5] == [
        "/webapi/Token",
        "/webapi/AntiForgery",
        "/webapi/login",
        "/webapi/Token",
        "/webapi/AntiForgery",
    ]
    login = next(r for r in fake.requests if r.url.path == "/webapi/login")
    assert json.loads(login.content)["Username"] == "kunde@example.com"
    assert login.headers["X-XSRF-TOKEN"] == "xsrf-1"
    basket = fake.requests[-1]
    assert basket.headers["Authorization"] == "Bearer bearer-2"  # fresh token after login


@pytest.fixture
def saved(monkeypatch, memory_keychain):
    """No env vars: the login comes from the keychain."""
    monkeypatch.delenv("HANDLE_NEMLIG_EMAIL")
    monkeypatch.delenv("HANDLE_NEMLIG_PASSWORD")
    return memory_keychain


def _saved_login(keychain):
    return json.loads(keychain.secrets[(registry.KEYCHAIN_SERVICE, "nemlig")])


async def test_saved_session_logs_in_without_a_password(store, fake, saved):
    registry.keychain_set("nemlig", "kunde@example.com", {".ASPXAUTH": "auth-1"})
    assert (await store.get_cart()).lines
    assert "/webapi/login" not in fake.paths()


async def test_dead_session_says_log_in_again(store, fake, saved):
    """A dead cookie gives an anonymous basket, not a 401: never shop anonymously."""
    registry.keychain_set("nemlig", "kunde@example.com", {".ASPXAUTH": "old"})
    with pytest.raises(StoreError, match="has expired. Run in a terminal: handle-mcp login nemlig"):
        await store.get_cart()
    assert "/webapi/login" not in fake.paths()


async def test_session_that_dies_mid_run_never_fills_an_anonymous_basket(store, fake, saved):
    registry.keychain_set("nemlig", "kunde@example.com", {".ASPXAUTH": "auth-1"})
    await store.get_cart()
    store._client.cookies.clear()  # nemlig drops the login: baskets turn anonymous, no 401
    with pytest.raises(StoreError, match="expired"):
        await store.set_quantities({"1": 2})
    assert "/webapi/basket/AddToBasket" not in fake.paths()


async def test_session_refused_on_every_call_says_log_in_again(store, fake, saved):
    registry.keychain_set("nemlig", "kunde@example.com", {".ASPXAUTH": "auth-1"})
    await store.get_cart()
    fake.scripted["/webapi/basket/GetBasket"] = [401, 401, 401]
    with pytest.raises(StoreError, match="expired"):
        await store.get_cart()


async def test_an_old_saved_password_is_replaced_by_the_session(store, fake, saved):
    saved.secrets[(registry.KEYCHAIN_SERVICE, "nemlig")] = json.dumps(
        {"email": "kunde@example.com", "password": "hemmeligt"}
    )
    await store.get_cart()
    assert _saved_login(saved) == {"email": "kunde@example.com", "session": {".ASPXAUTH": "auth-1"}}


async def test_env_vars_are_never_saved(store, fake, memory_keychain):
    await store.get_cart()
    assert store.session() == {".ASPXAUTH": "auth-1"}
    assert not memory_keychain.secrets


async def test_logs_in_only_once(store, fake):
    await store.get_cart()
    await store.get_cart()
    assert fake.paths().count("/webapi/login") == 1


async def test_wrong_password_is_a_clear_error(store, fake, monkeypatch):
    monkeypatch.setenv("HANDLE_NEMLIG_PASSWORD", "forkert")
    with pytest.raises(StoreError, match="Check your email and password"):
        await store.get_cart()


async def test_expired_token_is_renewed_once(store, fake):
    await store.get_cart()
    fake.scripted["/webapi/basket/GetBasket"] = [401]
    cart = await store.get_cart()
    assert cart.total == Decimal("182.45")
    assert fake.paths().count("/webapi/login") == 1  # token renewal was enough


async def test_retries_on_503_then_succeeds(store, fake):
    fake.scripted["/webapi/basket/GetBasket"] = [503, 503]
    assert (await store.get_cart()).lines


async def test_retries_on_network_error(store, fake):
    """Seen live: nemlig sometimes drops the connection (ReadError) during many searches."""
    await store.get_cart()
    fake.scripted["/webapi/basket/GetBasket"] = [httpx.ReadError("reset"), 503]
    assert (await store.get_cart()).lines


async def test_gives_up_after_retries(store, fake):
    fake.scripted["/webapi/basket/GetBasket"] = [503, 503, 503]
    with pytest.raises(StoreError, match="503"):
        await store.get_cart()


async def test_no_retry_on_400(store, fake):
    await store.get_cart()
    before = len(fake.requests)
    fake.scripted["/webapi/basket/GetBasket"] = [400]
    with pytest.raises(StoreError, match="400"):
        await store.get_cart()
    assert len(fake.requests) == before + 1


async def test_search_parses_products(store, fake):
    products = await store.search("cocio", limit=5)
    can, bottle = products
    assert (can.id, can.price, can.unit_price, can.unit, can.size) == (
        "5070417",
        Decimal("119.0"),
        Decimal("9.92"),
        "stk",
        "12 x 0,25 l / Classic",
    )
    assert not can.on_offer and can.available and can.ean is None
    assert can.category == "Drikke"
    assert bottle.unit == "l" and bottle.on_offer and bottle.offer_text == "3 for 68 kr"
    assert not bottle.available
    search = next(r for r in fake.requests if r.url.path == "/searchgateway/api/search")
    assert search.url.params["timeslotUtc"] == "2026110316-60-180"
    assert search.url.params["deliveryZoneId"] == "3"


async def test_search_reuses_settings(store, fake):
    await store.search("mælk")
    await store.search("brød")
    assert fake.paths().count("/webapi/v2/AppSettings/Website") == 1


async def test_cart_has_fees_minimum_and_warnings(store):
    cart = await store.get_cart()
    assert [(ln.product_id, ln.quantity) for ln in cart.lines] == [("5064654", 1), ("701025", 2)]
    assert cart.products_total == Decimal("147.45")
    assert cart.fees == {"levering": Decimal("29.0"), "poser": Decimal("6.0")}
    assert cart.total == Decimal("182.45")
    assert (cart.minimum_order, cart.minimum_met) == (Decimal("300.0"), False)
    assert cart.warnings == ["Minimumskøb er 300 kr."]
    assert cart.delivery == "Tirsdag 3. nov. kl. 17-20"


async def test_cart_hides_removed_lines(store, fake):
    fake.basket = {**fake.basket, "Lines": [*fake.basket["Lines"], {"Id": "9", "Name": "Æg",
        "Quantity": 0, "Price": 28.0}]}  # fmt: skip
    cart = await store.get_cart()
    assert "9" not in [ln.product_id for ln in cart.lines]
    assert cart.products_total == Decimal("147.45")  # nemlig's own total is kept


async def test_terms_estimate_bags_for_an_empty_cart(store, fake, monkeypatch):
    assert (await store.terms()).other_fees == {"poser": Decimal("6.0")}
    fake.basket = {**fake.basket, "TotalBagsPrice": 0}  # products but no bag fee: no guess
    assert (await store.terms()).other_fees == {}
    fake.basket = {**fake.basket, "Lines": [], "TotalBagsPrice": 0}
    assert (await store.terms()).other_fees == {"bags (estimated)": Decimal("17.85")}
    monkeypatch.setenv("HANDLE_NEMLIG_BAG_FEE", "12,50")
    assert (await store.terms()).other_fees == {"bags (estimated)": Decimal("12.50")}
    monkeypatch.setenv("HANDLE_NEMLIG_BAG_FEE", "ikke et tal")
    assert (await store.terms()).other_fees == {"bags (estimated)": Decimal("17.85")}


async def test_set_quantities_absolute_and_remove(store, fake):
    results = await store.set_quantities({"701025": 5, "5064654": 0})
    assert [(r.product_id, r.applied, r.ok) for r in results] == [
        ("701025", 5, True),
        ("5064654", 0, True),
    ]
    sent = [json.loads(r.content) for r in fake.requests if r.url.path.endswith("AddToBasket")]
    assert [(b["ProductId"], b["quantity"]) for b in sent] == [("701025", 5), ("5064654", 0)]


async def test_set_quantities_reports_refusal(store):
    [result] = await store.set_quantities({"sold-out": 2})
    assert (result.applied, result.ok) == (0, False)
    assert "not 2" in result.message


async def test_set_quantities_one_failure_does_not_stop_the_rest(store, fake):
    await store.get_cart()
    fake.scripted["/webapi/basket/AddToBasket"] = [400]
    results = await store.set_quantities({"1": 1, "2": 1})
    assert [r.ok for r in results] == [False, True]


async def test_terms_and_delivery_info(store, monkeypatch):
    monkeypatch.setattr(
        nemlig_delivery, "_now", lambda: datetime(2026, 11, 1, 12, tzinfo=nemlig_delivery.DK)
    )
    terms = await store.terms()
    assert (terms.home_delivery, terms.delivery_fee, terms.minimum_order) == (
        True,
        Decimal("29.0"),
        Decimal("300.0"),
    )
    info = await store.delivery_info()
    assert info.chosen_start.isoformat() == "2026-11-03T16:00:00+00:00"
    assert info.edit_deadline.isoformat() == "2026-11-02T23:00:00+00:00"
    assert "17-20" in info.note


async def test_nemlig_is_always_home_delivery(store):
    modes = {
        (x.delivery_mode, x.pickup_store)
        for x in (await store.get_cart(), await store.terms(), await store.delivery_info())
    }
    assert modes == {("home", None)}


async def test_an_ended_slot_is_not_the_chosen_one(store, monkeypatch):
    """nemlig keeps the last order's slot in the basket. Offers must not be priced for it."""
    monkeypatch.setattr(
        nemlig_delivery, "_now", lambda: datetime(2026, 11, 3, 20, 1, tzinfo=nemlig_delivery.DK)
    )
    info = await store.delivery_info()
    assert (info.chosen_start, info.chosen_end, info.edit_deadline) == (None, None, None)
    assert info.note.startswith("No time selected.")


@pytest.mark.parametrize(("hour", "ended"), [(19, False), (20, True)])
async def test_a_slot_without_a_zone_is_danish_time(store, fake, monkeypatch, hour, ended):
    fake.spot = {"TimeSlot": {"Start": "2026-11-03T17:00:00", "End": "2026-11-03T20:00:00"}}
    monkeypatch.setattr(
        nemlig_delivery, "_now", lambda: datetime(2026, 11, 3, hour, 1, tzinfo=nemlig_delivery.DK)
    )
    assert ((await store.delivery_info()).chosen_end is None) == ended


async def test_orders_and_order(store):
    [order] = await store.orders(limit=5)
    assert (order.id, order.status, order.total) == ("900001", "Leveret", Decimal("612.5"))
    assert order.delivery_start.hour == 15
    detail = await store.order("900001")
    assert [(ln.product_id, ln.quantity) for ln in detail.lines] == [("5064654", 1), ("701025", 3)]
    assert detail.total == Decimal("167.95")  # sum of lines when the answer has no total


@pytest.mark.parametrize("order_id", ["", "1/../basket/ClearBasket", "12?x=1", "١٢"])
async def test_order_id_must_be_digits(store, fake, order_id):
    """The id goes into the URL path: only digits, never a path or query from the model."""
    with pytest.raises(StoreError, match="order number must be numeric"):
        await store.order(order_id)
    assert not any("GetOrderHistory" in r.url.path for r in fake.requests)


async def test_rejected_call_does_not_show_the_url(store, fake):
    fake.scripted["/webapi/basket/GetBasket"] = [400]
    with pytest.raises(StoreError) as err:
        await store.get_cart()
    assert str(err.value) == "nemlig.com rejected the request (400)."


async def test_orders_with_flexible_delivery(store, fake):
    # Real answer: OrderDate is "03/11-2026 kl. 14-21", not a time. It gave a ValueError.
    fake.history, fake.details = "order_history_flex", "order_details_flex"
    [order] = await store.orders(limit=2)
    assert (order.id, order.total, order.placed_at) == ("1000001", Decimal("702.08"), None)
    assert (order.delivery_start.hour, order.delivery_end.hour) == (14, 21)
    detail = await store.order("1000001")
    assert len(detail.lines) == 2


async def test_never_calls_order_or_payment_endpoints(store, fake):
    await store.search("mælk")
    await store.get_cart()
    await store.set_quantities({"1": 1})
    await store.terms()
    await store.delivery_info()
    await store.orders()
    await store.order("900001")
    assert not [p for p in fake.paths() if "Checkout" in p or "PlaceOrder" in p]


async def test_tools_hide_personal_data_from_nemlig(store, monkeypatch):
    monkeypatch.setattr(registry, "STORES", {"nemlig": NemligStore})
    monkeypatch.setattr(server, "_instances", {"nemlig": store})
    text = json.dumps(
        [
            await server.get_cart("nemlig"),
            await server.get_delivery_info("nemlig"),
            await server.get_orders("nemlig"),
            await server.get_order("nemlig", "900001"),
        ],
        ensure_ascii=False,
    )
    for secret in (
        "Kunde Eksempel",
        "Testvej",
        "kunde@example.com",
        "kode-4711",
        "bagdøren",
        "98765432",
    ):
        assert secret not in text
    assert "Black Coffee Blend Supreme" in text


async def test_search_works_while_pages_are_in_the_queue(store, fake):
    fake.queue = True  # seen live 2026-11-03: /?GetAsJson=1 → queue-it, /webapi/ works
    assert await store.search("mælk")
    search = next(r for r in fake.requests if r.url.path == "/searchgateway/api/search")
    assert search.url.params["timeslotUtc"] == "2026110316-60-180"
    assert search.url.params["deliveryZoneId"] == "3"
    assert "/" not in fake.paths()


async def test_search_uses_defaults_when_settings_fail(store, fake):
    fake.scripted["/webapi/basket/GetBasket"] = [404]
    assert await store.search("mælk")
    search = next(r for r in fake.requests if r.url.path == "/searchgateway/api/search")
    assert search.url.params["timeslotUtc"] == "" and search.url.params["deliveryZoneId"] == "1"
    await store.search("brød")  # not cached: tries again
    assert fake.paths().count("/webapi/basket/GetBasket") == 2


async def test_offers_work_while_pages_are_in_the_queue(store, fake):
    fake.queue = True
    assert {p.campaign for p in await store.offers()} >= {"Skarp pris", "Priskup"}
    assert not {"/", "/tilbud"} & set(fake.paths())


async def test_queue_it_waiting_room_is_a_clear_error(store, fake):
    fake.api_queue = True
    with pytest.raises(StoreError, match="queue"):
        await store.offers()


# Shapes seen live 2026-11-01 (docs/research.md). Only the fields we read.
def test_discount_campaign_pays_the_campaign_price(store):
    hit = {
        "Id": 1,
        "Name": "Laksefilet m. skind",
        "Price": 120.75,
        "UnitPriceCalc": 201.25,
        "Campaign": {
            "Type": "ProductCampaignDiscountPercent",
            "DiscountPercent": 30,
            "CampaignPrice": 84.53,
            "CampaignUnitPrice": 140.88,
            "IntervalEnd": "2026-11-04T22:59:59Z",
        },
    }
    p = store._product(hit)
    assert (p.price, p.unit_price, p.before_price) == (
        Decimal("84.53"),
        Decimal("140.88"),
        Decimal("120.75"),
    )
    assert p.on_offer and p.offer_text == "-30 %"
    assert p.offer_until == date(2026, 11, 4)  # 22:59:59 UTC is the last minute of the day in DK


def test_discount_label_alone_is_not_an_offer(store):
    hit = {"Id": 2, "Name": "Letmælk 1,5%", "Price": 10.95, "Campaign": None, "DiscountItem": True}
    p = store._product(hit)
    assert (p.price, p.on_offer, p.offer_text, p.before_price) == (
        Decimal("10.95"),
        False,
        None,
        None,
    )


def test_multi_buy_keeps_the_single_price(store):
    campaign = {"Type": "ProductCampaignMixOffer", "MinQuantity": 4, "TotalPrice": 50.0}
    campaign["CampaignPrice"] = 50.0
    p = store._product({"Id": 3, "Name": "Hakkede tomater", "Price": 13.95, "Campaign": campaign})
    assert (p.price, p.offer_text, p.before_price) == (Decimal("13.95"), "4 for 50 kr", None)


def test_campaign_we_cannot_describe_is_still_an_offer(store):
    campaign = {"Type": "ProductCampaignBuyXForY", "MinQuantity": 3}  # no TotalPrice
    p = store._product({"Id": 4, "Name": "Letmælk", "Price": 21.28, "Campaign": campaign})
    assert (p.price, p.on_offer, p.offer_text) == (Decimal("21.28"), True, "Offer")


def test_plain_discount_says_the_before_price(store):
    campaign = {"Type": "ProductCampaignDiscount", "CampaignPrice": 79.95}
    p = store._product({"Id": 5, "Name": "Laks - back loin", "Price": 98.75, "Campaign": campaign})
    assert p.offer_text == "Offer, was 98,75 DKK"


def test_multi_buy_text_is_priced_by_compare(store):
    from handle_mcp.compare import best_match

    campaign = {"Type": "ProductCampaignMixOffer", "MinQuantity": 4, "TotalPrice": 50.0}
    hit = {"Id": 6, "Name": "Hakkede tomater", "Price": 13.95, "Description": "400 g"}
    p = store._product({**hit, "Campaign": campaign, "Availability": {}})
    assert best_match("4 x hakkede tomater 400 g", [p]).line_total == Decimal(50)


def test_offer_end_uses_danish_time(store):
    summer = {
        "Type": "ProductCampaignDiscount",
        "CampaignPrice": 10,
        "IntervalEnd": "2026-07-01T21:59:59Z",
    }
    p = store._product({"Id": 7, "Name": "Is", "Price": 20, "Campaign": summer})
    assert p.offer_until == date(2026, 7, 1)  # 23:59:59 in DK summer time


async def test_offers_reads_every_offer_list(store, fake):
    offers = await store.offers(10)
    assert {p.campaign for p in offers} == {
        "Skarp pris",
        "Priskup",
        "Dagstilbud",
        "Undgå madspild",
        "Free",
    }
    assert "5066321" not in {p.id for p in offers}  # "Frugt og grønt" is a category, not a list
    (page,) = [r for r in fake.requests if r.url.path == "/productbff/api/web/page"]
    assert page.url.params["path"] == "/tilbud"
    assert all(r.method == "GET" for r in fake.requests if r.url.path != "/webapi/login")
    steak = next(p for p in offers if p.id == "5067616")
    assert (steak.price, steak.before_price, steak.campaign, steak.unit) == (
        Decimal(100),
        Decimal("158.95"),
        "Skarp pris",
        "kg",
    )
    assert steak.offer_text == "Offer, was 158,95 DKK"
    ribs = next(p for p in offers if p.id == "5060880")  # no normal price given: still an offer
    assert (ribs.before_price, ribs.offer_text) == (None, "Skarp pris")
    click = next(p for p in offers if p.id == "5012574")
    assert (click.offer_text, click.campaign) == ("Mix 2 stk. 35,-", "Priskup")


async def test_offers_fit_the_basket_delivery_time(store, fake):
    await store.offers()
    (page,) = [r for r in fake.requests if r.url.path == "/productbff/api/web/page"]
    assert page.url.params["timeslotId"] == "2435672"


async def test_offers_without_a_delivery_time_send_none(store, fake):
    fake.basket = {k: v for k, v in fake.basket.items() if k != "DeliveryTimeSlot"}
    await store.offers()
    (page,) = [r for r in fake.requests if r.url.path == "/productbff/api/web/page"]
    assert "timeslotId" not in page.url.params


async def test_offers_limit_is_per_list(store):
    offers = await store.offers(1)
    assert len([p for p in offers if p.campaign == "Skarp pris"]) == 1


def test_multi_buy_text_is_the_offer():
    lemon = fixture("offers_page")["pageContent"][-1]["products"][1]  # "2 stk. 15,-"
    assert offer_product(lemon, "Priskup").offer_text == "2 stk. 15,-"


def test_offer_above_the_normal_price_is_not_an_offer():
    p = fixture("offers_page")["pageContent"][1]["products"][2] | {"price": 20000}
    assert offer_product(p, "Dagstilbud") is None


def test_offer_without_price_is_a_format_change():
    p = dict(fixture("offers_page")["pageContent"][1]["products"][2])
    del p["price"]
    with pytest.raises(KeyError):  # the list guard reports "ændret format", never "Gratis"
        offer_product(p, "Dagstilbud")


async def test_offers_limit_counts_only_real_offers(store, fake, monkeypatch):
    page = fixture("offers_page")
    block = next(b for b in page["pageContent"] if b.get("products"))
    first = block["products"][0] | {"price": 99999}  # not cheaper: filtered out
    block = block | {"products": [first, *block["products"][1:]]}
    page = page | {"pageContent": [block]}

    async def api(_method, url, **_kwargs):
        return page if url.endswith("/page") else {"Products": {"Products": []}}

    monkeypatch.setattr(store, "_api", api)
    assert len(await store.offers(1)) == 1


async def test_free_gift_costs_nothing_and_says_max_one(store):
    [gift] = await store.search("mums rosinboller")
    assert (gift.price, gift.before_price) == (Decimal(0), Decimal("20.45"))
    assert (gift.campaign, gift.max_per_order) == ("Free", 1)
    assert gift.offer_text == "Free (max 1 stk.)"


async def test_offer_list_gift_is_free_with_max_one(store):
    gift = next(p for p in await store.offers(10) if p.id == "5606321")
    assert (gift.price, gift.before_price, gift.campaign) == (
        Decimal(0),
        Decimal("20.45"),
        "Free",
    )
    assert (gift.offer_text, gift.max_per_order) == ("Free (max 1 stk.)", 1)


async def test_offers_without_lists_is_a_clear_error(store, fake, monkeypatch):
    monkeypatch.setattr(nemlig_offers, "OFFER_LISTS", {})
    with pytest.raises(StoreError, match="offer lists"):
        await store.offers()


async def test_one_broken_offer_list_does_not_hide_the_others(store, fake, monkeypatch):
    page = fixture("offers_page")
    page["pageContent"][1]["products"][0].pop("id")  # Skarp pris changed format
    monkeypatch.setattr(fake, "handle", _with_page(fake.handle, page))
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    offers = await store.offers(10)
    assert "Skarp pris" not in {p.campaign for p in offers} and offers


def _with_page(handle, page):
    def handler(request):
        if request.url.path == "/productbff/api/web/page":
            return httpx.Response(200, json=page)
        return handle(request)

    return handler


async def test_offers_find_free_gifts_by_search(store, fake):
    """Live 2026-11-02: "En gave til dig" was empty, but search "gratis" found the gift."""
    gift = next(p for p in await store.offers(10) if p.id == "5606321")
    assert (gift.price, gift.campaign, gift.max_per_order) == (Decimal(0), "Free", 1)
    searches = [r for r in fake.requests if r.url.path == "/searchgateway/api/search"]
    assert [r.url.params["query"] for r in searches] == ["gratis"]


async def test_offers_survive_a_failed_gift_search(store, fake):
    fake.scripted["/searchgateway/api/search"] = [500] * 3
    offers = await store.offers(10)
    assert offers and {p.campaign for p in offers} >= {"Skarp pris"}


async def test_delivery_slots_with_prices_and_kinds(store, fake):
    slots = await store.delivery_slots(days=7)
    tue = [s for s in slots if s.start.isoformat() == "2026-11-03T14:00:00+01:00"]
    assert {(s.kind, s.price) for s in tue if s.end.hour == 21} == {
        ("Personlig", Decimal("19.0")),
        ("Fleksibel", Decimal("25.0")),
    }
    wed = next(
        s
        for s in slots
        if s.start.isoformat() == "2026-11-04T14:00:00+01:00"
        and s.end.hour == 21
        and s.kind == "Personlig"
    )
    assert (wed.price, wed.available) == (Decimal("0.0"), True)
    assert not next(s for s in slots if s.start.hour == 7).available  # past deadline
    calls = [r for r in fake.requests if r.url.path.endswith("/GetDeliveryDays")]
    assert all(r.method == "GET" for r in calls)
    assert calls[1].url.params["startDate"] == "2026-11-06T00:00:00"  # next page for days 4-7
    assert calls[1].url.params["days"] == "4"


async def test_delivery_slots_stop_when_nemlig_keeps_paging(store, fake):
    fake.endless_days = True  # every page has 3 days and a next page
    await store.delivery_slots(days=7)
    assert fake.paths().count("/webapi/v2/Delivery/GetDeliveryDays") == 3


async def test_cheapest_chicken_is_far_down_in_nemlig_order(store):
    top = await store.search("kyllingebryst", 10)
    assert "450 g / tilsat vand / Rose" not in {p.size for p in top}  # the bug: hit 30


async def test_sorted_search_finds_the_cheapest_far_down(store, fake):
    by_unit = await compare.search(store, "kyllingebryst", 3, "unit_price")
    assert [p.size for p in by_unit] == [  # hits 31, 29 and 30 in nemlig's order
        "1,50 kg / tilsat vand / Rose",
        "1,20 kg / tilsat vand / Rose",
        "450 g / tilsat vand / Rose",
    ]
    search = [r for r in fake.requests if r.url.path == "/searchgateway/api/search"][-1]
    assert search.url.params["take"] == str(compare.SEARCH_LIMIT)
    by_price = await compare.search(store, "kyllingebryst", 5, "price")
    assert [p.price for p in by_price] == sorted(p.price for p in by_price)  # cold cuts first


@pytest.fixture
def no_login(monkeypatch, tmp_path):
    monkeypatch.delenv("HANDLE_NEMLIG_EMAIL")
    monkeypatch.delenv("HANDLE_NEMLIG_PASSWORD")
    monkeypatch.setattr(registry, "CONFIG_DIR", tmp_path)


async def test_offers_work_without_a_login(store, fake, no_login):
    """Headless setup, 2026-11-04: the offer lists are public, so no login must be needed.
    The gift search is skipped: nemlig's search gateway answers 500 to an anonymous token."""
    offers = await store.offers(10)
    assert {p.campaign for p in offers} >= {"Skarp pris", "Priskup"}
    assert "/webapi/login" not in fake.paths()
    assert "/webapi/basket/GetBasket" not in fake.paths()
    assert "/searchgateway/api/search" not in fake.paths()
    (page,) = [r for r in fake.requests if r.url.path == "/productbff/api/web/page"]
    assert page.headers["Authorization"] == "Bearer bearer-1"  # the anonymous token
    assert "timeslotId" not in page.url.params


async def test_search_needs_a_login(store, fake, no_login):
    """Live 2026-11-04: searchgateway answers 500 to an anonymous token, so say so up front."""
    with pytest.raises(StoreError, match="requires a login"):
        await store.search("mælk")
    assert fake.paths() == []


async def test_anonymous_token_is_renewed_once_when_it_expires(store, fake, no_login):
    await store.offers(5)
    fake.scripted["/productbff/api/web/page"] = [401]  # the token expired: one renewal
    assert await store.offers(5) and fake.paths().count("/webapi/Token") == 2
    fake.scripted["/productbff/api/web/page"] = [401, 401]  # still refused: a clear error
    with pytest.raises(StoreError, match="without a login"):
        await store.offers(5)


async def test_settings_from_anonymous_offers_are_refetched_after_login(
    store, fake, no_login, monkeypatch
):
    await store.offers(5)
    assert "/webapi/basket/GetBasket" not in fake.paths()
    monkeypatch.setenv("HANDLE_NEMLIG_EMAIL", "kunde@example.com")
    monkeypatch.setenv("HANDLE_NEMLIG_PASSWORD", "hemmeligt")
    await store.offers(5)
    assert "/webapi/basket/GetBasket" in fake.paths()  # the household's zone and time now


async def test_cart_still_needs_a_login(store, fake, no_login):
    with pytest.raises(registry.StoreError, match="No login saved for nemlig"):
        await store.get_cart()
