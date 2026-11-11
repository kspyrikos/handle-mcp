"""No personal data leaves the server, whatever a store adapter returns."""

import dataclasses
import json
from decimal import Decimal

import pytest

from handle_mcp import server
from handle_mcp.privacy import REDACTED, jsonable, scrub, scrub_text
from tests.conftest import LEAK

CALLS = [
    ("search_products", {"query": "mælk", "stores": ["fake"]}),
    ("list_offers", {"stores": ["fake"]}),
    ("search_recipes", {"query": "karry", "stores": ["fake"]}),
    ("get_recipe", {"store": "fake", "recipe": "r1", "persons": 6}),
    ("list_recipes", {"stores": ["fake"]}),
    ("list_recipes", {"stores": ["fake"], "tags": ["Familievenlig"]}),
    ("get_nutrition", {"store": "fake", "product_ids": ["1"]}),
    ("compare_basket", {"items": ["mælk 1 l", "2 x rugbrød"], "stores": ["fake"]}),
    ("get_cart", {"store": "fake"}),
    ("set_cart_quantities", {"store": "fake", "items": {"1": 1}}),
    ("add_recipe_to_cart", {"store": "fake", "recipe": "r1"}),
    ("get_delivery_info", {"store": "fake"}),
    ("list_delivery_slots", {"stores": ["fake"]}),
    ("get_orders", {"store": "fake"}),
    ("get_order", {"store": "fake", "order_id": "900000000001"}),
]


def _secrets(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _secrets(v)
    else:
        yield value


async def test_every_tool_is_checked():
    tested = {name for name, _ in CALLS}
    assert tested == {t.name for t in await server.mcp.list_tools()}


@pytest.mark.parametrize("tool, args", CALLS)
async def test_no_personal_data_in_tool_answers(tool, args):
    text = json.dumps(await getattr(server, tool)(**args), ensure_ascii=False)
    for secret in _secrets(LEAK):
        assert secret not in text, f"{tool} leaked {secret!r}"
    assert REDACTED in text


def test_scrub_nemlig_basket():
    basket = {
        "TotalPrice": 249.87,
        "Lines": [{"Id": "701025", "Name": "Kakaomælk", "Quantity": 2}],
        "DeliveryAddress": {"Name": "Anders And", "StreetName": "Vesterbrogade"},
        "InvoiceAddress": {"PostalCode": "1620"},
        "Email": "a@example.com",
        "DoorCode": "1234",
    }
    out = scrub(basket)
    assert out["Lines"] == basket["Lines"]  # product names stay
    assert out["TotalPrice"] == 249.87
    for key in ("DeliveryAddress", "InvoiceAddress", "Email", "DoorCode"):
        assert out[key] == REDACTED


def test_scrub_bilka_cart_matches_any_case():
    cart = {
        "stat": {"price": 12700},
        "uid": 123456,
        "tid": "abc",
        "deliveryAddress": {"recipient_name": "Anders", "zip": "7100"},
        "orders": [{"order_pickup_code": "87654321", "phone_number": "12345678"}],
    }
    out = scrub(cart)
    assert out["stat"] == {"price": 12700}
    assert out["uid"] == out["tid"] == out["deliveryAddress"] == REDACTED
    assert out["orders"][0] == {"order_pickup_code": REDACTED, "phone_number": REDACTED}


def test_scrub_does_not_change_input():
    data = {"Email": "a@example.com"}
    scrub(data)
    assert data == {"Email": "a@example.com"}


def test_scrub_dawa_address_id():
    cart = {"dawa_uid": "0a3f50a0-0000-0000-0000-000000000000", "stat": {"price": 1}}
    out = scrub(cart)
    assert out["dawa_uid"] == REDACTED
    assert out["stat"] == {"price": 1}


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal(0), "0.00"),
        (Decimal("0.0"), "0.00"),
        (Decimal(39), "39.00"),
        (Decimal("12.95"), "12.95"),
    ],
)
def test_money_is_always_two_decimals(value, text):
    """nemlig sends "0.0" and "32.0", BilkaToGo "0" and "39.00": one format across stores."""
    assert jsonable({"price": value}) == {"price": text}


def test_money_is_two_decimals_when_nested_and_rounded_half_up():
    @dataclasses.dataclass
    class Line:
        unit_price: Decimal

    out = jsonable({"fees": {"levering": Decimal(39)}, "lines": [Line(Decimal("0.125"))]})
    assert out == {"fees": {"levering": "39.00"}, "lines": [{"unit_price": "0.13"}]}


def test_money_that_cannot_be_quantized_is_still_shown():
    assert jsonable(Decimal("NaN")) == "NaN"
    assert jsonable(Decimal("1e40")) == "1E+40"


@pytest.mark.parametrize(
    "text",
    [
        "Kontakt anders@example.com om ordren",
        "Ring på +45 98 76 54 32",
        "Ring på 98765432",
        "Levering til Kildevej 12B",
        "Levering til H.C. Andersens Boulevard 2",
        "2100 København Ø",
        "Levering til Strandvejen 3",  # definite forms
        "Levering til Søndergaden 5",
        "Levering til Birkealléen 7B",
        "Levering til Rådhuspladsen 1",
        "Levering til Åboulevarden 12",
    ],
)
def test_scrub_text_removes_contact_and_address(text):
    out = scrub_text(text)
    assert REDACTED in out
    for word in ("anders@", "98 76", "98765432", "Kildevej", "Boulevard 2", "2100", "en 3"):
        assert word not in out


@pytest.mark.parametrize(
    "text",
    [
        "Du mangler 123,45 kr. for at nå minimumsbeløbet",
        "Kildevand 0,5 l 24 stk",
        "Max 6 stk. pr. ordre",
        "Varen 5064654 er udsolgt",
        "EAN 5701234567890",
        "Bestil senest 04/11 kl. 23:59",
        "Levering til Nytorvet i morgen",
    ],
)
def test_scrub_text_leaves_store_messages_alone(text):
    assert scrub_text(text) == text


def test_free_text_under_harmless_keys_is_scrubbed():
    """Key-based redaction cannot see a name in `warnings`: the value is scrubbed too."""
    cart = {
        "name": "Mælk 1 l, Kildevej Mejeri",  # a product name passes as the store wrote it
        "warnings": ["Ring til os på 98765432", "Adresse: Kildevej 12, 2100 København"],
        "results": [{"message": "Sendt til anders@example.com"}],
    }
    out = scrub(cart)
    assert out["name"] == cart["name"]
    text = json.dumps(out, ensure_ascii=False)
    for word in ("98765432", "Kildevej 12", "2100", "anders@"):
        assert word not in text


def test_notes_are_free_text():
    assert scrub({"notes": "Stil den ved Søndergaden 5"}) == {"notes": f"Stil den ved {REDACTED}"}


def test_free_text_in_dicts_under_a_free_text_key_is_scrubbed():
    out = scrub({"messages": [{"text": "Ring 98765432"}], "warnings": {"detail": "a@b.dk"}})
    assert out == {"messages": [{"text": f"Ring {REDACTED}"}], "warnings": {"detail": REDACTED}}


@pytest.mark.parametrize("text", ["Varen 12345678 er udsolgt", "Max 1000 Stk pr. ordre"])
def test_scrub_text_false_positives_are_accepted(text):
    """An 8-digit id looks like a phone, "1000 Stk" like a postal code: hidden on purpose."""
    assert REDACTED in scrub_text(text)


async def test_recipe_text_passes_scrub_unchanged():
    """get_recipe's photo link, intro, method and tip are the store's recipe text, not personal
    data: _out() keeps them as the store wrote them."""
    from handle_mcp.models import RecipeDetails, RecipeSection

    recipe = RecipeDetails(
        store="nemlig",
        id="kebabfad-med-fladbrod-98001303",
        name="Kebabfad med fladbrød",
        url="https://www.nemlig.com/maaltider/kebabfad-med-fladbrod-98001303",
        persons=4,
        description="Prøv denne lækre og nemme opskrift på kebabfad 🌯",
        image="https://media.umbraco.io/nemlig3/zu0cfnyn/kebabfad.jpg?mode=crop",
        ingredients_persons=4,
        method=[RecipeSection(title="Kebabfad", steps=["Tænd ovnen på 200 grader varmluft"])],
        tip="Server med citron",
    )
    out = await server._out(recipe)
    assert out == jsonable(recipe)
    assert out["method"] == [{"title": "Kebabfad", "steps": ["Tænd ovnen på 200 grader varmluft"]}]
