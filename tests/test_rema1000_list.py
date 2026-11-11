"""REMA 1000's cart is the shopping list "handle-mcp" (docs/adr/0007), against a fake API."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from handle_mcp import stores as registry
from handle_mcp import telemetry
from handle_mcp.stores import rema1000_list
from handle_mcp.stores.rema1000 import Rema1000Store
from tests.test_rema1000_login import STORES, jwt

NOW = datetime(2026, 11, 10, 9, tzinfo=UTC)
TOKEN = jwt(NOW + timedelta(hours=1), sub=4242)
CATALOG = {
    "20302": {"name": "SØDMÆLK 3,5%", "pricing": {"price": 7.95, "deposit": 0}},
    "100": {"name": "COLA", "pricing": {"price": 10, "deposit": 3}},
    "200": {
        "name": "RUGBRØD",
        "pricing": {"price": 10, "is_on_discount": True, "max_quantity": 2, "price_over_max": 15},
    },
}


class FakeLists:
    """The user's lists. 7 is the human's own primary list: it must never be touched."""

    def __init__(self) -> None:
        self.lists: dict[int, dict] = {
            7: {"id": 7, "name": "Indkøb", "primary": True, "items": [self.line(1, "20302", 2)]}
        }
        self.next = 1000
        self.writes: list[httpx.Request] = []
        self.lost: list[str] = []  # the next POSTs: "lost" (landed, answer lost) or "dropped"
        self.lag = 0  # list reads that do not show a new list yet

    def line(self, line_id: int, pid: str, n: int, bought: bool = False) -> dict:
        item = {"id": line_id, "amount": n, "bought": bought, "store_item_id": int(pid)}
        return item | {"store_item": CATALOG[pid] | {"id": int(pid)}}

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/")
        if path == "v3/stores":
            return httpx.Response(200, json=STORES)
        assert req.headers["authorization"] == f"Bearer {TOKEN}"
        if req.method != "GET":
            self.writes.append(req)
        body = json.loads(req.content) if req.content else None
        if path == "v3/users/4242/shopping-lists":
            rows = [
                {"id": str(lst["id"]), "name": lst["name"], "is_primary": lst["primary"]}
                for lst in self.lists.values()
                if not (self.lag and lst["id"] >= 1000)
            ]
            self.lag = max(self.lag - 1, 0)
            return httpx.Response(
                200, json={"data": rows, "meta": {"pagination": {"last_page": 1}}}
            )
        parts = path.split("/")
        if parts[:2] != ["v2", "shoppinglists"]:
            return httpx.Response(404)
        if len(parts) == 2 and req.method == "POST":
            return self.post(lambda: self.create(body))
        lst = self.lists.get(int(parts[2]))
        if lst is None:
            return httpx.Response(404)
        if len(parts) == 3 and req.method == "GET":
            assert req.url.params["with"] == "items"  # never with=users
            return httpx.Response(200, json=lst)
        if len(parts) == 3 and req.method == "PATCH":
            drop = {i["id"] for i in body["items"] if i["delete"] is True}
            lst["items"] = [i for i in lst["items"] if i["id"] not in drop]
            return httpx.Response(200, json=lst)
        if len(parts) == 4 and req.method == "POST":
            pid = str(body["store_item_id"])
            if pid not in CATALOG:
                return httpx.Response(422, json={"message": "unknown"})
            assert body["store_id"] == 1
            return self.post(lambda: self.add(lst, pid, body["amount"]))
        if len(parts) == 5 and req.method == "PATCH":
            item = next(i for i in lst["items"] if i["id"] == int(parts[4]))
            item.update(body)
            return httpx.Response(200, json=item)
        return httpx.Response(405)

    def post(self, do) -> httpx.Response:
        fate = self.lost.pop(0) if self.lost else "ok"
        if fate == "dropped":
            return httpx.Response(502)
        do()
        if fate == "lost":
            raise httpx.ReadTimeout("lost")
        return httpx.Response(201, json={})

    def create(self, body: dict) -> None:
        assert body == {"name": "handle-mcp", "primary": False, "items": []}
        self.next += 1
        self.lists[self.next] = {
            "id": self.next,
            "name": body["name"],
            "primary": False,
            "items": [],
        }

    def add(self, lst: dict, pid: str, n: int) -> None:
        self.next += 1
        lst["items"].append(self.line(self.next, pid, n))

    def mine(self) -> dict:
        return next(lst for lst in self.lists.values() if lst["name"] == "handle-mcp")


@pytest.fixture
def api():
    return FakeLists()


@pytest.fixture
def store(api, monkeypatch):
    s = Rema1000Store(client=httpx.AsyncClient(transport=httpx.MockTransport(api.handle)))
    monkeypatch.setattr(s, "_now", lambda: NOW)
    monkeypatch.setattr(rema1000_list, "WAIT", 0)
    session = {"access_token": TOKEN, "refresh_token": "r1", "store_ids": "263"}
    registry.keychain_set("rema1000", "a@example.com", session)
    return s


async def test_no_list_is_an_empty_cart(store, api):
    cart = await store.get_cart()
    assert cart.lines == [] and cart.total == 0 and "does not exist yet" in cart.warnings[0]
    assert cart.delivery_mode == "pickup" and cart.pickup_store == "Aalborg C, Hasseris"
    assert api.writes == []  # reading never creates the list


async def test_first_write_creates_the_list_never_primary(store, api):
    results = await store.set_quantities({"20302": 2, "200": 3})
    assert [(r.applied, r.ok) for r in results] == [(2, True), (3, True)]
    assert not api.mine()["primary"] and api.lists[7]["items"][0]["amount"] == 2
    cart = await store.get_cart()
    assert [(li.product_id, li.quantity) for li in cart.lines] == [("20302", 2), ("200", 3)]
    # 2 x 7.95 + offer: 2 x 10 and 1 over the limit at 15
    assert cart.total == Decimal("15.90") + Decimal(35) and "pay yourself" in cart.warnings[0]


async def test_never_touches_another_list(store, api):
    await store.set_quantities({"20302": 1})
    await store.set_quantities({"20302": 0, "100": 4})
    mine = api.mine()["id"]
    for req in api.writes:
        path = req.url.path
        assert req.method in ("POST", "PATCH")  # never DELETE: a line goes with delete: true
        assert path == "/api/v2/shoppinglists" or path.startswith(f"/api/v2/shoppinglists/{mine}")
    assert api.lists[7]["items"][0]["amount"] == 2 and api.lists[7]["primary"]


async def test_absolute_quantities_and_zero_removes(store, api):
    await store.set_quantities({"20302": 2})
    await store.set_quantities({"20302": 5})
    assert [i["amount"] for i in api.mine()["items"]] == [5]
    results = await store.set_quantities({"20302": 0})
    assert results[0].ok and api.mine()["items"] == []
    assert len([lst for lst in api.lists.values() if lst["name"] == "handle-mcp"]) == 1


async def test_removing_from_no_list_writes_nothing(store, api):
    assert (await store.set_quantities({"20302": 0}))[0].ok and api.writes == []


async def test_deposit_is_a_fee(store, api):
    await store.set_quantities({"100": 2})
    cart = await store.get_cart()
    assert cart.products_total == 20 and cart.fees == {"pant": 6} and cart.total == 26


async def test_bought_lines_are_left_out_and_removed_on_the_next_write(store, api):
    await store.set_quantities({"20302": 2, "100": 1})
    api.mine()["items"][0]["bought"] = True  # ticked off in the app
    cart = await store.get_cart()
    assert [li.product_id for li in cart.lines] == ["100"]
    await store.set_quantities({"100": 3})
    assert [(i["store_item_id"], i["amount"]) for i in api.mine()["items"]] == [(100, 3)]


async def test_a_bought_product_asked_again_comes_back(store, api):
    await store.set_quantities({"20302": 2})
    api.mine()["items"][0]["bought"] = True
    results = await store.set_quantities({"20302": 1})
    assert results[0].ok and len(api.mine()["items"]) == 1
    line = api.mine()["items"][0]
    assert line["amount"] == 1 and line["bought"] is False


async def test_two_lines_sum_and_the_next_write_makes_one(store, api):
    await store.set_quantities({"20302": 1})
    api.add(api.mine(), "20302", 2)  # a lagging read let a second POST through
    cart = await store.get_cart()
    assert [(li.product_id, li.quantity) for li in cart.lines] == [("20302", 3)]
    assert (await store.set_quantities({"20302": 4}))[0].ok
    assert [i["amount"] for i in api.mine()["items"]] == [4]


async def test_two_lists_use_the_lowest_id_and_warn_by_count(store, api):
    api.lists[9] = {
        "id": 9,
        "name": "handle-mcp",
        "primary": False,
        "items": [api.line(90, "100", 1)],
    }
    api.lists[10] = {"id": 10, "name": "handle-mcp", "primary": False, "items": []}
    cart = await store.get_cart()
    assert [li.product_id for li in cart.lines] == ["100"]  # 9, not 10: ids compared as numbers
    assert any("1 more list(s)" in w for w in cart.warnings)
    assert all("Indkøb" not in w for w in cart.warnings)
    await store.set_quantities({"20302": 1})
    assert api.lists[10]["items"] == [] and len(api.lists) == 3  # nothing deleted


async def test_lost_answer_is_read_before_a_second_post(store, api):
    api.lost = ["lost", "lost"]  # the list POST and the line POST both landed, answers lost
    results = await store.set_quantities({"20302": 2})
    assert results[0].ok
    assert len([lst for lst in api.lists.values() if lst["name"] == "handle-mcp"]) == 1
    assert len(api.mine()["items"]) == 1


async def test_dropped_post_is_sent_again(store, api):
    api.lost = ["ok", "dropped"]  # the line POST got a 502 and did not land
    assert (await store.set_quantities({"20302": 2}))[0].ok
    assert [i["amount"] for i in api.mine()["items"]] == [2]


async def test_post_that_never_lands_fails_that_product_only(store, api):
    await store.set_quantities({"100": 1})
    api.lost = ["dropped", "dropped", "dropped"]
    results = await store.set_quantities({"100": 3, "20302": 2})
    assert results[0].ok and results[0].applied == 3
    assert not results[1].ok and "was not added to the list" in results[1].message


async def test_items_null_and_odd_last_page(store, api):
    real = api.handle

    def odd(req: httpx.Request) -> httpx.Response:
        resp = real(req)
        path = req.url.path
        if path.endswith("/shopping-lists"):
            data = json.loads(resp.content)
            data["meta"]["pagination"]["last_page"] = "1"
            return httpx.Response(200, json=data)
        if req.method == "GET" and path.startswith("/api/v2/shoppinglists/"):
            return httpx.Response(200, json=json.loads(resp.content) | {"items": None})
        return resp

    store._client = httpx.AsyncClient(transport=httpx.MockTransport(odd))
    await store.set_quantities({"20302": 0})
    api.lists[1001] = {"id": 1001, "name": "handle-mcp", "primary": False, "items": []}
    cart = await store.get_cart()
    assert cart.lines == []


async def test_lagging_read_after_create(store, api):
    api.lost, api.lag = ["lost"], 1  # the new list shows on the second read
    await store.set_quantities({"20302": 1})
    assert len([lst for lst in api.lists.values() if lst["name"] == "handle-mcp"]) == 1


async def test_unknown_product_is_refused_and_the_rest_goes_in(store, api):
    results = await store.set_quantities(
        {"999": 1, "20302": 1, "x": 1, "²": 1, "020302": 1, "0": 1}
    )
    assert [r.ok for r in results] == [False, True, False, False, False, False]
    assert len(api.mine()["items"]) == 1  # "020302" added no second line
    assert "rejected the product (422)" in results[0].message and "Invalid product ID" in results[2].message
    assert "Invalid product ID" in results[3].message


async def test_failed_cleanup_still_reports_each_product(store, api):
    await store.set_quantities({"20302": 1, "100": 1})
    real = api.handle

    def refuse_cleanup(req: httpx.Request) -> httpx.Response:
        if req.method == "PATCH" and req.url.path.count("/") == 4:  # the list itself
            return httpx.Response(422)
        return real(req)

    store._client = httpx.AsyncClient(transport=httpx.MockTransport(refuse_cleanup))
    results = await store.set_quantities({"20302": 3, "100": 0})
    assert results[0].ok and "did not remove" in results[0].message
    assert not results[1].ok and "The list has 1" in results[1].message


async def test_store_without_click_and_collect_still_has_the_list(store, api):
    registry.keychain_set(
        "rema1000",
        "a@example.com",
        {"access_token": TOKEN, "refresh_token": "r1", "store_ids": "329"},
    )
    await store.set_quantities({"20302": 1})
    cart = await store.get_cart()
    assert cart.pickup_store is None and cart.lines


async def test_list_is_counted_as_list_filled(monkeypatch):
    sent = []
    monkeypatch.setattr(telemetry, "_send", lambda event, data, wait=False: sent.append(event))
    monkeypatch.setattr(telemetry, "_pending", {"rema1000": (_Timer(), {"store": "rema1000"})})
    telemetry._flush("rema1000")
    assert sent == ["list_filled"]


class _Timer:
    def cancel(self) -> None:
        pass
