"""REMA 1000 pickup: the stores the human chose at login, and their Køb & Hent times. Read only.

`handle-mcp login rema1000` lets the human pick one or more stores, in order. The choice is
saved on this machine with the session; HANDLE_REMA1000_STORE_ID (ids, comma-separated)
overrides it. Nothing is set at REMA. A store without Køb & Hent can be chosen: the first
chosen store that has it gives the pickup store and the times (docs/adr/0007). The store list
is read again after an hour, so a store that gets Køb & Hent shows up by itself.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from handle_mcp.models import DeliveryInfo, DeliverySlot, StoreTerms
from handle_mcp.stores import StoreError, has_login, load_login

log = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Copenhagen")
STORE_ENV = "HANDLE_REMA1000_STORE_ID"
RECHECK = timedelta(hours=1)
STORE_PAGES = 10  # 1000 stores: REMA has about 450
SHOWN = 40  # most stores listed at once on the login terminal
KIND = "Køb & Hent"
NOTE = (
    "REMA 1000 only supports Køb & Hent pickup or in-store shopping; it does not deliver. "
    "Køb & Hent has a packing fee based on order size; REMA 1000 does not provide the fee here."
)
NO_TIME = "Choose and pay for the time at REMA 1000. handle-mcp does not select a time."


class GateClosed(StoreError):
    """No chosen store has Køb & Hent, or none is chosen. Search and the list still work."""


def no_store() -> GateClosed:
    return GateClosed("No REMA 1000 store selected. Run in a terminal: handle-mcp login rema1000")


@dataclass
class Pickup:
    id: int
    name: str
    earlier: bool  # a store the human ranked higher has no Køb & Hent yet


def parse_ids(raw: str) -> list[int]:
    """ "9100, 9200" as ids, in order. Not a positive number, or a repeat: dropped."""
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0 and int(part) not in out:
            out.append(int(part))
    return out


def judge(ids: list[int], rows: list[dict]) -> Pickup | GateClosed:
    """The first chosen store, in the human's order, that exists and has Køb & Hent."""
    by_id = {row.get("id"): row for row in rows}
    earlier = False
    for i in ids:
        row = by_id.get(i)
        if row is None:
            continue
        if row.get("is_click_and_collect_active") is True:
            return Pickup(i, str(row.get("name") or "").strip(), earlier)
        earlier = True
    if earlier:
        return GateClosed(
            "None of your selected REMA 1000 stores supports Køb & Hent yet. Search and "
            "the shopping list still work. Pickup times will appear as soon as one of them supports it."
        )
    return GateClosed(
        "Your selected REMA 1000 stores are no longer available. Choose again: handle-mcp login rema1000"
    )


def label(row: dict) -> str:
    """One line of the login list: name, postcode and city, Køb & Hent yes or no."""
    place = f"{row.get('postal_code') or ''} {row.get('city') or ''}".strip()
    text = str(row.get("name") or "").strip() + (f", {place}" if place else "")
    yes = "yes" if row.get("is_click_and_collect_active") is True else "no"
    return f"{text}. Køb & Hent: {yes}"


class RemaPickupMixin:
    """The store gate, pickup times, terms and delivery info. Needs `_get()` from
    Rema1000Store and `_api_json()` and `_now()` from RemaAuthMixin."""

    name: str
    _stores: str
    _gate: tuple[tuple[int, ...], datetime, Pickup | GateClosed] | None

    def _chosen(self) -> list[int]:
        """The chosen stores: the env var, else the saved session."""
        if raw := os.environ.get(STORE_ENV, "").strip():
            return parse_ids(raw)
        if not self._stores and has_login(self.name):
            self._stores = load_login(self.name).get("session", {}).get("store_ids", "")
        return parse_ids(self._stores)

    def choose_stores(self, ids: list[int]) -> None:
        """For `handle-mcp login`: the human's picks, saved with the session."""
        self._stores = ",".join(str(i) for i in ids)
        self._gate = None

    async def _all_stores(self, want: list[int] | None = None) -> list[dict]:
        """REMA's public store list, paged. Stops early once every id in `want` is seen."""
        rows: list[dict] = []
        for page in range(1, STORE_PAGES + 1):
            data = await self._get("v3/stores", {"per_page": 100, "page": page})
            rows += [r for r in data["data"] if isinstance(r, dict)]
            seen = {r.get("id") for r in rows}
            last = ((data.get("meta") or {}).get("pagination") or {}).get("last_page") or 1
            if page >= last or not data["data"] or (want and seen >= set(want)):
                break
        return rows

    async def store_choices(self, query: str) -> list[tuple[int, str]]:
        """For `handle-mcp login`: stores whose name, postcode or city holds `query`."""
        q = query.strip().lower()
        found = []
        for row in await self._all_stores():
            text = f"{row.get('name')} {row.get('postal_code')} {row.get('city')}".lower()
            if isinstance(row.get("id"), int) and q in text:
                found.append((row["id"], label(row)))
        return found[:SHOWN]

    async def _pickup(self) -> Pickup:
        ids = self._chosen()
        if not ids:
            raise no_store()
        key, now = tuple(ids), self._now()
        if self._gate and self._gate[0] == key and now - self._gate[1] < RECHECK:
            verdict = self._gate[2]
        else:
            verdict = judge(ids, await self._all_stores(ids))
            self._gate = (key, now, verdict)
        if isinstance(verdict, GateClosed):
            raise GateClosed(str(verdict))
        return verdict

    async def _slots(self, pickup: Pickup) -> list[DeliverySlot]:
        data = await self._api_json(f"v3/stores/{pickup.id}/time-slots", "pickup times")
        days = data.get("data")
        if not isinstance(days, dict):  # no times: REMA answers []
            return []
        note = f"Pickup at {pickup.name}. REMA 1000 does not provide the fee here."
        out = []
        for slots in days.values():
            for slot in slots if isinstance(slots, list) else []:
                try:
                    start = datetime.fromisoformat(slot["from"]).astimezone(TZ)
                    end = datetime.fromisoformat(slot["to"]).astimezone(TZ)
                except (KeyError, TypeError, ValueError) as exc:
                    raise StoreError(
                        "REMA 1000 returned a pickup slot with an invalid time. "
                        "Its API may have changed."
                    ) from exc
                available = slot.get("is_available") is True
                out.append(DeliverySlot(self.name, start, end, None, available, KIND, note))
        return sorted(out, key=lambda s: s.start)

    async def delivery_slots(self, days: int = 7) -> list[DeliverySlot]:
        """Pickup times in the pickup store, today and the next days. Read only."""
        pickup = await self._pickup()
        first = self._now().astimezone(TZ).date()
        until = first + timedelta(days=max(days, 1))
        return [s for s in await self._slots(pickup) if first <= s.start.date() < until]

    async def delivery_info(self) -> DeliveryInfo:
        try:
            pickup = await self._pickup()
        except GateClosed as exc:  # no store chosen: no nagging, search works without one
            note = f"{NOTE} {exc}" if self._chosen() else NOTE
            return DeliveryInfo(store=self.name, note=note, delivery_mode="pickup")
        note = f"Køb & Hent pickup at {pickup.name}. {NO_TIME} {NOTE.split('. ', 1)[1]}"
        if pickup.earlier:
            note += f" Your preferred store does not support Køb & Hent yet; using {pickup.name}."
        nxt: date | None = None
        if has_login(self.name):  # the times need a login; the store list does not
            try:
                nxt = next((s.start.date() for s in await self._slots(pickup) if s.available), None)
            except StoreError as exc:  # the store and the note still hold
                note += f" Could not read the pickup times: {exc}"
        else:
            note += " Log in to see pickup times: handle-mcp login rema1000"
        return DeliveryInfo(
            store=self.name, next_available=nxt, note=note, delivery_mode="pickup",
            pickup_store=pickup.name,
        )  # fmt: skip

    async def terms(self) -> StoreTerms:
        """Pickup, no home delivery. No fee or minimum is known, so they are None. A failed
        store check never stops compare_basket: the pickup store is then unknown."""
        try:
            pickup: Pickup | None = await self._pickup()
        except StoreError as exc:
            if not isinstance(exc, GateClosed):
                log.warning("rema1000 store check failed: %s", type(exc).__name__)
            pickup = None
        return StoreTerms(
            store=self.name, home_delivery=False, delivery_mode="pickup",
            pickup_store=pickup.name if pickup else None,
        )  # fmt: skip
