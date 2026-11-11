"""The one live WRITE test: REMA 1000's shopping list "handle-mcp" (docs/adr/0007).

Run on a machine with `handle-mcp login rema1000`:
    HANDLE_LIVE=1 HANDLE_LIVE_WRITE=1 uv run pytest -m live -s tests/test_live_rema_list.py
It adds one product to the list, reads it back, and sets it back to what it was. Only the list
"handle-mcp" is touched: never an order, a time, a payment or another list. Look at the list in
REMA's app afterwards.
"""

import os

import pytest

from handle_mcp.stores.rema1000 import Rema1000Store
from handle_mcp.stores.rema1000_list import LIST

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("HANDLE_LIVE") != "1" or os.environ.get("HANDLE_LIVE_WRITE") != "1",
        reason="set HANDLE_LIVE=1 and HANDLE_LIVE_WRITE=1",
    ),
]


def _count(cart, pid: str) -> int:
    return sum(line.quantity for line in cart.lines if line.product_id == pid)


async def test_list_round_trip():
    store = Rema1000Store()
    milk = (await store.search("sødmælk", limit=1))[0]
    before = _count(await store.get_cart(), milk.id)
    print(f"\n[rema1000] {milk.name} ({milk.id}) på listen før: {before}")

    up = await store.set_quantities({milk.id: before + 1})
    assert up[0].ok, up
    cart = await store.get_cart()
    assert _count(cart, milk.id) == before + 1
    assert not any("primary" in w for w in cart.warnings), cart.warnings
    print(f"[rema1000] efter +1: {before + 1}. Total {cart.total} kr. Advarsler: {cart.warnings}")

    down = await store.set_quantities({milk.id: before})
    assert down[0].ok, down
    assert _count(await store.get_cart(), milk.id) == before
    print(f'[rema1000] tilbage på {before}. Se listen "{LIST}" i REMA-appen.')
