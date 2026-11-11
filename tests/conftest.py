import os
from dataclasses import asdict, field, make_dataclass
from datetime import date, datetime
from decimal import Decimal

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from handle_mcp import server, telemetry
from handle_mcp import stores as registry
from handle_mcp.models import (
    Cart,
    CartLine,
    DeliveryInfo,
    DeliverySlot,
    Nutrition,
    Order,
    Product,
    ProductNutrition,
    QuantityResult,
    Recipe,
    RecipeDetails,
    RecipeItem,
    RecipeProduct,
    StoreTerms,
)


def pytest_configure(config):
    """Live tests log in for real. pytest's long tracebacks print every frame's arguments, and a
    failed login call carries the email and password (e.g. Bilka's Gigya form). So live runs get
    short tracebacks and no locals: a failure must never put a login in a log or a paste."""
    if os.environ.get("HANDLE_LIVE") == "1":
        config.option.tbstyle = "short"
        config.option.showlocals = False


# Personal data a store adapter could leak by mistake. Every tool must remove it.
LEAK = {
    "CustomerName": "Anders And",
    "Email": "anders@example.com",
    "MobileNumber": "+45 98 76 54 32",
    "DoorCode": "kode-4711",
    "UnattendedNotes": "Nøglen ligger under måtten",
    "deliveryAddress": {"street": "Andeby 1", "zip": "postnr-9999", "city": "Andeby"},
}

# A slot with LEAK as extra fields: the tool sorts slots by attribute, so they cannot be dicts.
LeakySlot = make_dataclass(
    "LeakySlot",
    [(k, object, field(default_factory=lambda v=v: v)) for k, v in LEAK.items()],
    bases=(DeliverySlot,),
)


class FakeStore:
    """A store that answers from memory. Smuggles LEAK into every answer."""

    name = "fake"
    cart_page = "https://example.com/kurv?ref=1"

    def __init__(self) -> None:
        self.cart: dict[str, int] = {"1": 2}

    def _leak(self, obj):
        """Return the model as a dict with personal data mixed in, like a sloppy adapter."""
        return {**asdict(obj), **LEAK}

    async def search(self, query, limit=10):
        return [
            self._leak(
                Product(
                    "fake",
                    "1",
                    f"{query} 1 l",
                    Decimal("12.95"),
                    unit_price=Decimal("12.95"),
                    unit="l",
                    ean="5701234567890",
                )
            )
        ]

    async def offers(self, limit=25):
        return await self.search("Tilbud", limit)

    async def get_cart(self):
        lines = [CartLine(pid, "Mælk", q, Decimal("12.95") * q) for pid, q in self.cart.items()]
        total = sum((line.line_total for line in lines), Decimal(0))
        return self._leak(
            Cart(
                "fake",
                lines,
                total,
                {"levering": Decimal(39)},
                total + 39,
                minimum_order=Decimal(500),
                minimum_met=total >= 500,
            )
        )

    async def set_quantities(self, items):
        self.cart.update(items)
        self.cart = {k: v for k, v in self.cart.items() if v}
        return [QuantityResult(pid, q, q, True) for pid, q in items.items()]

    async def delivery_info(self):
        return self._leak(
            DeliveryInfo(
                "fake",
                chosen_start=datetime(2026, 11, 3, 16),
                chosen_end=datetime(2026, 11, 3, 19),
                next_available=date(2026, 11, 2),
            )
        )

    async def delivery_slots(self, days=7):
        tue, wed = datetime(2026, 11, 3, 14), datetime(2026, 11, 4, 14)
        return [
            LeakySlot("fake", tue, tue.replace(hour=21), Decimal(25), kind="Fleksibel"),
            DeliverySlot("fake", tue, tue.replace(hour=21), Decimal(19), kind="Personlig"),
            DeliverySlot("fake", wed, wed.replace(hour=21), Decimal(0), kind="Personlig"),
            DeliverySlot("fake", wed, wed.replace(hour=17), Decimal(0), available=False),
        ]

    async def terms(self):
        return StoreTerms("fake", True, Decimal(39), minimum_order=Decimal(500))

    async def nutrition(self, product_ids):
        label = Nutrition("100 g", energy_kcal="55", protein="8.6", salt="0.13")
        return [self._leak(ProductNutrition("fake", i, "Skyr", label)) for i in product_ids]

    async def recipes(self, query, limit=5):
        return [self._leak(Recipe("fake", "r1", f"{query} til 4", "https://example.com/r1"))]

    async def recipe(self, recipe_id, persons=None):
        milk = RecipeProduct("1", "Mælk", Decimal("12.95"), 2, "1 l")
        return self._leak(
            RecipeDetails(
                "fake",
                recipe_id,
                "Grød",
                "https://example.com/r1",
                persons or 4,
                items=[RecipeItem("mælk", milk)],
                total=Decimal("25.90"),
            )
        )

    async def recipes_by_tag(self, tags, category=None, limit=20):
        return [
            self._leak(Recipe("fake", "r2", "Pasta", "https://example.com/r2", group="Aftensmad"))
        ]

    async def featured_recipes(self, limit=20):
        return [self._leak(Recipe("fake", "r1", "Grød", "https://example.com/r1", group="Morgen"))]

    async def orders(self, limit=10):
        return [self._leak(Order("fake", "900000000001", "Leveret", Decimal("612.50")))]

    def session(self):
        return {"token": "session-1"}

    async def order(self, order_id):
        return self._leak(
            Order(
                "fake",
                order_id,
                "Leveret",
                Decimal("612.50"),
                lines=[CartLine("1", "Mælk", 2, Decimal("25.90"))],
            )
        )


class BrokenStore(FakeStore):
    name = "broken"

    async def search(self, query, limit=10):
        raise registry.StoreError("API'et svarer 500")

    async def orders(self, limit=10):
        raise NotImplementedError

    async def nutrition(self, product_ids):
        raise NotImplementedError


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch, tmp_path):
    """Tests never send an event. tests/test_telemetry.py turns it on with a fake sender."""
    monkeypatch.setenv("HANDLE_TELEMETRY", "0")
    # Keep existing store-data assertions deterministic; translation has dedicated tests.
    monkeypatch.setenv("HANDLE_LANGUAGE", "da")
    monkeypatch.setattr(telemetry, "_off", False)
    monkeypatch.setattr(telemetry, "_sent_today", {})
    monkeypatch.setattr(telemetry, "_noticed", False)
    monkeypatch.setattr(telemetry, "_pending", {})
    monkeypatch.setattr(registry, "CONFIG_DIR", tmp_path / "config")


@pytest.fixture(autouse=True)
def fake_stores(monkeypatch):
    monkeypatch.setattr(registry, "STORES", {"fake": FakeStore, "broken": BrokenStore})
    monkeypatch.setattr(server, "_instances", {})


class MemoryKeyring(KeyringBackend):
    """A keychain in memory, so tests never read or write the real one."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.secrets: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.secrets.get((service, username))

    def set_password(self, service, username, password):
        self.secrets[(service, username)] = password

    def delete_password(self, service, username):
        if self.secrets.pop((service, username), None) is None:
            raise PasswordDeleteError("not found")


@pytest.fixture(autouse=True)
def memory_keychain(request):
    if request.node.get_closest_marker("live"):  # live tests use the real keychain
        yield None
        return
    real = keyring.get_keyring()
    fake = MemoryKeyring()
    keyring.set_keyring(fake)
    yield fake
    keyring.set_keyring(real)
