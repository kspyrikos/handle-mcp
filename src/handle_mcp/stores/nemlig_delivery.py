"""nemlig.com delivery: the chosen time and the time picker's list. Read only: the human
chooses the time on nemlig.com (docs/adr/0001)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from handle_mcp.models import DeliveryInfo, DeliverySlot

log = logging.getLogger(__name__)

WEB = "https://www.nemlig.com/webapi"
DK = ZoneInfo("Europe/Copenhagen")
SLOT_KINDS = {0: "Personlig", 1: "Fleksibel"}  # Type: attended / unattended delivery


def _money(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _time(value: str | None) -> datetime | None:
    """An ISO time, or None. nemlig sometimes sends text: OrderDate "03/11-2026 kl. 14-21"."""
    if not value or value.startswith(("0001-", "9999-")):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        log.debug("nemlig: not a time: %r", value)
        return None


def _now() -> datetime:
    return datetime.now(DK)


class DeliveryMixin:
    """Needs `name`, `_basket()` and `_api()` from NemligStore."""

    async def delivery_info(self) -> DeliveryInfo:
        basket = await self._basket()
        spot = await self._api("GET", f"{WEB}/Order/DeliverySpot") or {}
        slot = spot.get("TimeSlot") or {}
        chosen = basket.get("FormattedDeliveryTime")
        start, end, deadline = (
            _time(slot.get("Start")),
            _time(slot.get("End")),
            _time(spot.get("EditDeadline")),
        )
        if end and end.replace(tzinfo=end.tzinfo or DK) < _now():  # the last order's slot
            start, end, deadline, chosen = None, None, None, None
        return DeliveryInfo(
            store=self.name,
            delivery_mode="home",
            chosen_start=start,
            chosen_end=end,
            edit_deadline=deadline,
            note=(
                f"Selected time in the cart: {chosen}. The fee depends on the time (approximately 19-51 DKK)."
                if chosen
                else "No time selected. The fee depends on the time (approximately 19-51 DKK). Choose a time on nemlig.com."
            ),
        )

    async def delivery_slots(self, days: int = 7) -> list[DeliverySlot]:
        """The time picker's list (GET). Logged in, so prices are for our zone. Never choose a
        slot here: the human does (docs/adr/0001)."""
        slots: list[DeliverySlot] = []
        start, seen = None, 0
        for _page in range(days):  # every page has at least one day, so this is the cap
            params = {"days": days - seen, "showForSubscriptions": "false", "startDate": start}
            url = f"{WEB}/v2/Delivery/GetDeliveryDays"
            data = await self._api("GET", url, params={k: v for k, v in params.items() if v})
            day_list = (data or {}).get("DayRangeHours") or []
            for day in day_list:
                slots += [self._slot(h) for h in day.get("DayHours") or []]
            seen, start = seen + len(day_list), (data or {}).get("NextRangeStart")
            if not day_list or not start or seen >= days:
                break
        return [s for s in slots if s]

    def _slot(self, hour: dict) -> DeliverySlot | None:
        day, deadline = _time(hour.get("Date")), _time(hour.get("Deadline"))
        if day is None or None in (hour.get("StartHour"), hour.get("EndHour")):
            return None
        return DeliverySlot(
            store=self.name,
            start=day.replace(tzinfo=DK) + timedelta(hours=hour["StartHour"]),
            end=day.replace(tzinfo=DK) + timedelta(hours=hour["EndHour"]),
            price=_money(hour.get("DeliveryPrice")),
            available=hour.get("Availability") == 0,  # 1 past deadline, 2 sold out, 3 off
            kind=SLOT_KINDS.get(hour.get("Type")),
            note=f"Bestil senest {deadline:%d/%m kl. %H:%M}" if deadline else None,
        )
