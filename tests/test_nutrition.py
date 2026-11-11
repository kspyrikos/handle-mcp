"""Nutrition labels: the parser, both stores against fakes (httpx.MockTransport), the tool."""

import json
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from handle_mcp import nutrition, server
from handle_mcp.models import Nutrition
from handle_mcp.stores import StoreError, _http
from handle_mcp.stores.bilkatogo import BilkaStore
from handle_mcp.stores.nemlig import NemligStore
from handle_mcp.stores.rema1000 import Rema1000Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    """A 500 is retried with a pause (stores/_http.py): no pause in tests."""

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", no_sleep)


def fixture(store: str, name: str) -> dict:
    return json.loads((FIXTURES / store / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("0,13 g", "0.13"),
        ("4,0 g", "4.0"),
        ("35 g", "35"),
        ("1.017 kJ", "1017"),
        ("2.1 g", "2.1"),  # a dot decimal (nemlig's old page JSON)
        ("0.125 g", "0.125"),
        ("<0,5 g", "<0.5"),
        ("< 0,01 g", "<0.01"),
        ("under 0,1 g", "<0.1"),
        ("12.345,6 kJ", "12345.6"),
        ("spor", None),
        ("", None),
    ],
)
def test_number_reads_danish_values(text, value):
    assert nutrition.number(text) == value


def test_energy_reads_one_or_both_units():
    assert nutrition.energy("329 kJ / 78 Kcal") == {"energy_kj": "329", "energy_kcal": "78"}
    assert nutrition.energy("233 kJ") == {"energy_kj": "233"}
    assert nutrition.energy("55 kcal") == {"energy_kcal": "55"}
    assert nutrition.energy("329") == {}


@pytest.mark.parametrize(
    ("text", "per"),
    [
        ("Næringsindhold pr. 100 ml", "100 ml"),
        ("100 g", "100 g"),
        ("100g", "100 g"),
        ("pr. portion", None),
        (None, None),
    ],
)
def test_basis(text, per):
    assert nutrition.basis(text) == per


def test_label_maps_every_line_and_skips_unsaturated_fat():
    rows = [
        ("Energi", "1.017 kJ"),
        ("Energi", "243 kcal"),
        ("Fedt", "6,5 g"),
        ("- heraf mættede fedtsyrer", "0,7 g"),
        ("- heraf enkeltumættede fedtsyrer", "3,1 g"),
        ("Kulhydrater", "35 g"),
        ("- heraf sukkerarter", "2,4 g"),
        ("Kostfibre", "7,5 g"),
        ("Protein", "7,0 g"),
        ("Salt", "1,2 g"),
    ]
    assert nutrition.label(rows, "100 g") == Nutrition(
        per="100 g",
        energy_kj="1017",
        energy_kcal="243",
        fat="6.5",
        saturated_fat="0.7",
        carbohydrate="35",
        sugar="2.4",
        protein="7.0",
        salt="1.2",
        fibre="7.5",
    )


def test_label_without_known_lines_is_none():
    assert nutrition.label([], "100 g") is None
    assert nutrition.label([("Netto mængde", "1 kg")], None) is None


# --- BilkaToGo -----------------------------------------------------------------------


def _bilka(seen: list) -> BilkaStore:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": [fixture("bilkatogo", "algolia_nutrition")]})

    return BilkaStore(httpx.AsyncClient(transport=httpx.MockTransport(handle)))


async def test_bilka_reads_every_id_in_one_request_without_a_login():
    seen: list[httpx.Request] = []
    answer = await _bilka(seen).nutrition(["19753", "120041", "20824", "999"])
    assert len(seen) == 1 and "Authorization" not in seen[0].headers
    params = json.loads(seen[0].content)["requests"][0]["params"]
    assert "objectID%3A19753+OR+objectID%3A120041" in params and "infos" in params
    assert "isInAssortmentIn" not in params  # no address: no login needed
    skyr, mince, bread, unknown = answer
    assert skyr.name.startswith("Skyr m. vanilje") and skyr.reason is None
    assert skyr.nutrition == Nutrition(
        per="100 g",
        energy_kj="233",
        energy_kcal="55",
        fat="0.2",
        saturated_fat="0.1",
        carbohydrate="4.0",
        sugar="3.6",
        protein="8.6",
        salt="0.13",
    )
    assert mince.nutrition is None and "nutrition label" in mince.reason
    assert bread.nutrition.energy_kj == "1017" and bread.nutrition.fibre == "7.5"
    assert (unknown.id, unknown.nutrition, unknown.reason) == (
        "999",
        None,
        "BilkaToGo does not recognize this product.",
    )


async def test_bilka_rejects_ids_that_are_not_item_numbers():
    with pytest.raises(StoreError, match="Unknown product ID"):
        await _bilka([]).nutrition(["19753) OR (x"])


async def test_bilka_format_change_is_a_clear_error():
    store = BilkaStore(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    )
    with pytest.raises(StoreError, match="changed format"):
        await store.nutrition(["19753"])


# --- nemlig --------------------------------------------------------------------------


def _nemlig(answers: dict) -> tuple[NemligStore, list]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/webapi/Token":
            return httpx.Response(200, json={"access_token": "anon"})
        if path == "/webapi/AntiForgery":
            return httpx.Response(200, json={"Value": "xsrf"})
        pid = path.rsplit("/", 1)[-1]
        answer = answers.get(pid, 404)
        return (
            httpx.Response(answer) if isinstance(answer, int) else httpx.Response(200, json=answer)
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return NemligStore(client), seen


@pytest.fixture
def no_nemlig_login(monkeypatch):
    monkeypatch.setattr(NemligStore, "_has_login", lambda self: False)


async def test_nemlig_reads_the_declaration_table(no_nemlig_login):
    store, seen = _nemlig(
        {
            "5070417": fixture("nemlig", "product_details_cocio"),
            "5027322": fixture("nemlig", "product_details_flanksteak"),
        }
    )
    cocio, steak, unknown = await store.nutrition(["5070417", "5027322", "123"])
    assert cocio.name == "Cocio kakaomælk (dåse)"
    assert cocio.nutrition == Nutrition(
        per="100 ml",
        energy_kj="329",
        energy_kcal="78",
        fat="2.1",
        saturated_fat="1.3",
        carbohydrate="11",
        sugar="11",
        protein="3.6",
        salt="0.11",
    )
    assert steak.nutrition is None and "nutrition label" in steak.reason
    assert (unknown.nutrition, unknown.reason) == (None, "nemlig does not recognize this product.")
    calls = [r for r in seen if "productDetails" in r.url.path]
    assert len(calls) == 3 and all(r.headers["Authorization"] == "Bearer anon" for r in calls)


async def test_nemlig_whole_failure_says_why(no_nemlig_login):
    store, _ = _nemlig({"1": 500, "2": 500})
    with pytest.raises(StoreError, match="500"):
        await store.nutrition(["1", "2"])


async def test_nemlig_one_failure_keeps_the_others(no_nemlig_login):
    store, _ = _nemlig({"5070417": fixture("nemlig", "product_details_cocio"), "2": 500})
    cocio, broken = await store.nutrition(["5070417", "2"])
    assert cocio.nutrition is not None
    assert broken.nutrition is None and "500" in broken.reason


async def test_nemlig_rejects_ids_that_are_not_product_numbers():
    store, seen = _nemlig({})
    with pytest.raises(StoreError, match="Unknown product ID"):
        await store.nutrition(["../basket"])
    assert seen == []


# --- the tool ------------------------------------------------------------------------


async def test_tool_answers_strings_not_money():
    answer = await server.get_nutrition("fake", ["1", "1", " "])
    (product,) = answer["products"]  # duplicates and blanks dropped
    assert product["nutrition"]["salt"] == "0.13"  # a string as on the label, not "0.13" money
    assert product["nutrition"]["fat"] is None


@pytest.mark.parametrize(
    ("ids", "match"), [([], "No product ID"), ([str(i) for i in range(51)], "At most 50")]
)
async def test_tool_rejects_bad_input(ids, match):
    with pytest.raises(ToolError, match=match):
        await server.get_nutrition("fake", ids)


async def test_tool_in_a_store_without_labels_is_polite():
    with pytest.raises(ToolError, match="broken does not support this yet"):  # NotImplementedError
        await server.get_nutrition("broken", ["1"])


# --- REMA 1000 -----------------------------------------------------------------------


def _rema(answers: dict) -> tuple[Rema1000Store, list]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = answers.get(request.url.path.rsplit("/", 1)[-1], 404)
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error_type": "NotFound"})
        return httpx.Response(200, json=fixture("rema1000", answer))

    return Rema1000Store(httpx.AsyncClient(transport=httpx.MockTransport(handle))), seen


async def test_rema_reads_nutrition_info_without_a_login():
    store, seen = _rema(
        {"20385": "product_skyr", "20758": "product_letmaelk", "400171": "product_oksekoed"}
    )
    skyr, milk, mince, unknown = await store.nutrition(["20385", "20758", "400171", "1"])
    assert skyr.name == "SKYR VANILJE"
    assert skyr.nutrition == Nutrition(
        per="100 g",  # compare unit kg
        energy_kj="318",
        energy_kcal="75",
        fat="<0.5",
        saturated_fat="<0.1",
        carbohydrate="8.6",
        sugar="8.6",
        protein="9.6",
        salt="0.14",
        fibre="<0.5",
    )
    assert milk.nutrition.per == "100 ml"  # compare unit ltr
    assert mince.nutrition is None and "nutrition label" in mince.reason
    assert (unknown.nutrition, unknown.reason) == (None, "REMA 1000 does not recognize this product.")
    assert all(r.url.params["include"] == "nutrition_info" for r in seen)
    assert not any("Authorization" in r.headers or "cookie" in r.headers for r in seen)


async def test_rema_whole_failure_says_why():
    store, _ = _rema({"1": 500, "2": 500})
    with pytest.raises(StoreError, match="500"):
        await store.nutrition(["1", "2"])


async def test_rema_rejects_ids_that_are_not_product_numbers():
    store, seen = _rema({})
    with pytest.raises(StoreError, match="Unknown product ID"):
        await store.nutrition(["../lists"])
    assert seen == []


async def test_rema_one_failure_keeps_the_others():
    store, _ = _rema({"20385": "product_skyr", "2": 500, "400171": "product_oksekoed"})
    skyr, broken, mince = await store.nutrition(["20385", "2", "400171"])
    assert skyr.nutrition is not None and mince.reason
    assert broken.nutrition is None and broken.reason == "REMA 1000 returned HTTP 500. Try again later."


async def test_nemlig_unknown_and_failing_products_get_their_own_reason(no_nemlig_login):
    store, _ = _nemlig({"5070417": fixture("nemlig", "product_details_cocio"), "2": 500})
    cocio, broken, unknown = await store.nutrition(["5070417", "2", "3"])
    assert cocio.nutrition is not None
    assert broken.reason == "nemlig.com returned HTTP 500. Try again later."
    assert unknown.reason == "nemlig does not recognize this product."


async def test_only_unknown_products_is_an_answer_not_an_error(no_nemlig_login):
    store, _ = _nemlig({})
    (unknown,) = await store.nutrition(["3"])
    assert unknown.reason == "nemlig does not recognize this product."


async def test_tool_keeps_the_order_and_drops_duplicates():
    answer = await server.get_nutrition("fake", [" 2", "1", "2", ""])
    assert [p["id"] for p in answer["products"]] == ["2", "1"]
