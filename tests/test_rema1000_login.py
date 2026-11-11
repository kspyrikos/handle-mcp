"""REMA 1000 login, renewal, the store choice and pickup times, against a fake site. No network."""

import asyncio
import base64
import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from handle_mcp import cli
from handle_mcp import stores as registry
from handle_mcp.stores import StoreError
from handle_mcp.stores import rema1000_auth as auth
from handle_mcp.stores.rema1000 import NOTE, Rema1000Store
from handle_mcp.stores.rema1000_pickup import GateClosed, parse_ids

FIXTURES = Path(__file__).parent / "fixtures" / "rema1000"
NOW = datetime(2026, 11, 10, 9, tzinfo=UTC)
STORES = json.loads((FIXTURES / "stores.json").read_text(encoding="utf-8"))
OFFERS = json.loads((FIXTURES / "offers.json").read_text(encoding="utf-8"))


def jwt(exp: datetime, sub: int = 4242) -> str:
    def part(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'sub': str(sub), 'exp': int(exp.timestamp())})}.sig"


SLOTS = {
    "data": {
        "2026-11-11": [
            {"from": "2026-11-11T15:00:00Z", "to": "2026-11-11T17:00:00Z", "is_available": True},
            {"from": "2026-11-11T13:00:00Z", "to": "2026-11-11T15:00:00Z", "is_available": False},
        ],
        "2026-11-10": [
            {"from": "2026-11-10T16:00:00Z", "to": "2026-11-10T18:00:00Z", "is_available": False}
        ],
        "2026-11-30": [
            {"from": "2026-11-30T16:00:00Z", "to": "2026-11-30T18:00:00Z", "is_available": True}
        ],
    }
}


class FakeSite:
    """shop, login, cphapp and the API, as the webshop sees them."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.access = jwt(NOW + timedelta(hours=1))
        self.renewed = jwt(NOW + timedelta(hours=2))
        self.login_status = 200
        self.login_next = "http://login.rema1000.dk/authorize?code=1"  # REMA answers http://
        self.refresh_status = 200
        self.api_401 = 0  # how many API calls answer 401 first
        self.slots: dict = SLOTS

    def calls(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        host, path = req.url.host, req.url.path
        if host == "shop.rema1000.dk" and path == "/konto/auth":
            return httpx.Response(302, headers={"Location": "https://login.rema1000.dk/?r=1"})
        if host == "login.rema1000.dk" and path == "/":
            return httpx.Response(200, text="<html>login</html>")
        if host == "cphapp.rema1000.dk" and path == "/api/oauth2/login":
            assert json.loads(req.content) == {"email": "a@example.com", "password": "hemmelig"}
            if self.login_status != 200:
                return httpx.Response(self.login_status, json={"message": "nej"})
            return httpx.Response(200, json={"redirect_uri": self.login_next})
        if host == "login.rema1000.dk" and path == "/authorize":
            where = "https://shop.rema1000.dk/api/auth/callback/rema?code=1"
            return httpx.Response(302, headers={"Location": where})
        if host == "shop.rema1000.dk" and path == "/api/auth/callback/rema":
            cookies = [
                f"access_token={self.access}",
                "refresh_token=r1",
                "api_session=s1",
                "AWSALB=lb1",
                "AWSALBCORS=lb1",
            ]
            headers = [("Location", "/konto")]
            headers += [("Set-Cookie", f"{c}; Domain=rema1000.dk; Path=/") for c in cookies]
            return httpx.Response(302, headers=headers)
        if host == "shop.rema1000.dk" and path == "/api/auth/refresh":
            assert "refresh_token=r1" in req.headers.get("cookie", "")
            if self.refresh_status != 200:
                return httpx.Response(self.refresh_status)
            headers = [
                ("Set-Cookie", f"access_token={self.renewed}; Path=/"),
                ("Set-Cookie", "refresh_token=r2; Path=/"),
            ]
            return httpx.Response(200, headers=headers)
        if host == "api.digital.rema1000.dk":
            if path == "/api/v3/stores":
                return httpx.Response(200, json=STORES)
            if path == "/api/search/products":
                return httpx.Response(200, json=OFFERS)
            if req.headers.get("authorization") not in (
                f"Bearer {self.access}",
                f"Bearer {self.renewed}",
            ) or (self.api_401 and not self._spend()):
                return httpx.Response(401, json={"error_type": "Unauthorized"})
            if path.endswith("/time-slots"):
                return httpx.Response(200, json=self.slots)
        return httpx.Response(404)

    def api_only(self, req: httpx.Request) -> httpx.Response:
        """The client for search and the API: the login never runs on it (its cookies would)."""
        assert req.url.host == "api.digital.rema1000.dk", req.url.host
        return self.handle(req)

    def _spend(self) -> bool:
        self.api_401 -= 1
        return False


@pytest.fixture
def site():
    return FakeSite()


@pytest.fixture
def store(site, monkeypatch):
    s = Rema1000Store(
        httpx.AsyncClient(transport=httpx.MockTransport(site.api_only)),
        httpx.AsyncClient(transport=httpx.MockTransport(site.handle)),
    )
    monkeypatch.setattr(s, "_now", lambda: NOW)
    return s


@pytest.fixture
def env_login(monkeypatch):
    monkeypatch.setenv("HANDLE_REMA1000_EMAIL", "a@example.com")
    monkeypatch.setenv("HANDLE_REMA1000_PASSWORD", "hemmelig")


def saved(site: FakeSite, **extra: str) -> None:
    session = {
        "access_token": site.access,
        "refresh_token": "r1",
        "api_session": "s1",
        "AWSALB": "lb1",
        "AWSALBCORS": "lb1",
    }
    registry.keychain_set("rema1000", "a@example.com", session | extra)


async def test_login_keeps_the_cookies_never_the_password(store, site, env_login):
    await store.check_login()
    session = store.session()
    assert session["access_token"] == site.access and session["refresh_token"] == "r1"
    assert {"api_session", "AWSALB", "AWSALBCORS"} <= set(session)
    assert session["access_exp"] == str(int((NOW + timedelta(hours=1)).timestamp()))
    assert "hemmelig" not in json.dumps(session) and "a@example.com" not in json.dumps(session)
    assert store._user == 4242
    login = next(r for r in site.requests if r.url.path == "/api/oauth2/login")
    assert login.headers["origin"] == "https://login.rema1000.dk"
    assert login.headers["referer"] == "https://login.rema1000.dk/?r=1"
    assert site.calls("/authorize") == 1  # http:// became https://
    assert site.calls("/konto") == 0  # stops at the callback
    assert not store._client.cookies.jar and not store._auth.cookies.jar  # in the session only


@pytest.mark.parametrize("status", [400, 401, 422])
async def test_wrong_password(store, site, env_login, status):
    site.login_status = status
    with pytest.raises(StoreError, match="rejected the login"):
        await store.check_login()
    assert store.session() is None


@pytest.mark.parametrize(
    "where, error",
    [
        ("https://rema.queue-it.net/?c=1", "queue or bot check"),
        ("https://evil.example.com/authorize", "redirected the login unexpectedly"),
        ("http://", "redirected the login unexpectedly"),
    ],
)
async def test_login_never_goes_elsewhere(store, site, env_login, where, error):
    site.login_next = where
    with pytest.raises(StoreError, match=error):
        await store.check_login()


async def test_bot_check_on_login(store, site, env_login):
    site.login_status = 403
    with pytest.raises(StoreError, match="bot check"):
        await store.check_login()


async def test_saved_session_is_used_without_a_login(store, site):
    saved(site, store_ids="263")
    slots = await store.delivery_slots(7)
    assert slots and site.calls("/api/oauth2/login") == 0 and site.calls("/api/auth/refresh") == 0
    call = next(r for r in site.requests if r.url.path.endswith("/time-slots"))
    assert call.headers["authorization"] == f"Bearer {site.access}"
    assert "cookie" not in call.headers  # the API takes the token only


async def test_ended_token_is_renewed_and_saved(store, site):
    site.access = jwt(NOW - timedelta(minutes=1))
    saved(site, store_ids="263")
    await store.delivery_slots(7)
    assert site.calls("/api/auth/refresh") == 1
    again = registry.keychain_get("rema1000")["session"]
    assert again["access_token"] == site.renewed and again["refresh_token"] == "r2"
    assert again["api_session"] == "s1" and again["store_ids"] == "263"  # kept


async def test_refused_renewal_means_log_in_again(store, site):
    site.access, site.refresh_status = jwt(NOW - timedelta(minutes=1)), 401
    saved(site, store_ids="263")
    with pytest.raises(StoreError, match="expired"):
        await store.delivery_slots(7)


async def test_renewal_5xx_is_a_store_error_and_not_retried(store, site):
    site.access, site.refresh_status = jwt(NOW - timedelta(minutes=1)), 502
    saved(site, store_ids="263")
    with pytest.raises(StoreError, match="HTTP 502 during session renewal"):
        await store.delivery_slots(7)
    assert site.calls("/api/auth/refresh") == 1


async def test_401_renews_once(store, site):
    saved(site, store_ids="263")
    site.api_401 = 1
    assert await store.delivery_slots(7)
    assert site.calls("/api/auth/refresh") == 1


async def test_second_401_means_expired(store, site):
    saved(site, store_ids="263")
    site.api_401 = 5
    with pytest.raises(StoreError, match="expired"):
        await store.delivery_slots(7)
    assert site.calls("/api/auth/refresh") == 1


async def test_session_without_tokens_is_expired(store):
    registry.keychain_set("rema1000", "a@example.com", {"api_session": "s1"})
    with pytest.raises(StoreError, match="expired"):
        await store.check_login()


def test_claims_and_numbers():
    assert auth.claims("not.a.jwt!") == {} and auth.claims("abc") == {}
    assert auth.number("12") == 12 and auth.number(1.9) == 1
    assert auth.number(True) is None and auth.number("nan") is None and auth.number(-1) is None


# The pickup store


def test_parse_ids():
    assert parse_ids(" 9100, 9200,x,9100,-3,0") == [9100, 9200]


async def test_pickup_is_the_first_chosen_store_with_click_and_collect(store, site):
    saved(site, store_ids="329,263")  # 329 has no Køb & Hent
    info = await store.delivery_info()
    assert info.pickup_store == "Aalborg C, Hasseris" and info.delivery_mode == "pickup"
    assert info.next_available == date(2026, 11, 11)
    assert "Your preferred store does not support Køb & Hent yet" in info.note
    assert "does not select a time" in info.note
    terms = await store.terms()
    assert terms.pickup_store == "Aalborg C, Hasseris" and not terms.home_delivery
    assert site.calls("/api/v3/stores") == 1  # checked once an hour


async def test_store_without_click_and_collect_is_allowed(store, site):
    saved(site, store_ids="329")
    info = await store.delivery_info()
    assert info.pickup_store is None and "Køb & Hent yet" in info.note
    assert (await store.terms()).pickup_store is None
    with pytest.raises(GateClosed, match="shopping list still work"):
        await store.delivery_slots(7)


async def test_gone_store_asks_to_choose_again(store, site):
    saved(site, store_ids="1")
    with pytest.raises(GateClosed, match="Choose again"):
        await store.delivery_slots(7)


async def test_no_login_no_store_no_call(store, site):
    assert (await store.delivery_info()).note == NOTE  # no nagging: search needs no store
    assert (await store.terms()).pickup_store is None
    with pytest.raises(GateClosed, match="No REMA 1000 store selected"):
        await store.delivery_slots(7)
    assert site.requests == []


async def test_env_store_without_login_shows_the_store_not_times(store, site, monkeypatch):
    monkeypatch.setenv("HANDLE_REMA1000_STORE_ID", "263")
    info = await store.delivery_info()
    assert info.pickup_store == "Aalborg C, Hasseris" and info.next_available is None
    assert "Log in to see pickup times" in info.note
    assert all(r.url.host == "api.digital.rema1000.dk" for r in site.requests)


async def test_store_is_checked_again_after_an_hour(store, site, monkeypatch):
    saved(site, store_ids="263")
    await store.terms()
    monkeypatch.setattr(store, "_now", lambda: NOW + timedelta(minutes=61))
    await store.terms()
    assert site.calls("/api/v3/stores") == 2


async def test_slots_in_danish_time_sorted_and_in_range(store, site):
    saved(site, store_ids="263")
    slots = await store.delivery_slots(2)
    assert [s.start.isoformat() for s in slots] == [
        "2026-11-10T17:00:00+01:00",
        "2026-11-11T14:00:00+01:00",
        "2026-11-11T16:00:00+01:00",
    ]
    assert [s.available for s in slots] == [False, False, True]
    assert slots[0].price is None and slots[0].kind == "Køb & Hent"
    assert "Aalborg C, Hasseris" in slots[0].note and "fee" in slots[0].note


async def test_no_slots(store, site):
    saved(site, store_ids="263")
    site.slots = {"data": []}
    assert await store.delivery_slots(7) == []


async def test_bad_slot_time(store, site):
    saved(site, store_ids="263")
    site.slots = {"data": {"2026-11-11": [{"from": "i morgen", "to": "x"}]}}
    with pytest.raises(StoreError, match="invalid time"):
        await store.delivery_slots(7)


async def test_store_choices_search_name_postcode_and_city(store):
    assert [i for i, _ in await store.store_choices("aalborg")] == [263, 265]
    assert [i for i, _ in await store.store_choices("8000")] == [270]
    (_, text), *_ = await store.store_choices("Hasseris")
    assert text == "Aalborg C, Hasseris, 9000 Aalborg. Køb & Hent: yes"


# `handle-mcp login rema1000` on a terminal


async def test_choose_on_the_terminal(store, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("ingen-by\naalborg\n2,1,9\n"))
    await cli._choose(store)
    assert store._stores == "265,263"
    assert "Selected: Aalborg C, Østerbro" in capsys.readouterr().err


async def test_skip_the_choice(store, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    await cli._choose(store)
    assert store._stores == "" and "No store selected" in capsys.readouterr().err


def test_login_saves_session_and_stores(site, monkeypatch, capsys):
    transport = httpx.MockTransport(site.handle)
    clients = (httpx.AsyncClient(transport=transport), httpx.AsyncClient(transport=transport))
    monkeypatch.setattr(registry, "STORES", {"rema1000": lambda: Rema1000Store(*clients)})
    monkeypatch.setattr("sys.stdin", io.StringIO("a@example.com\nhasseris\n1\n"))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "hemmelig")
    assert cli.run(["login", "rema1000"]) == 0
    session = registry.keychain_get("rema1000")["session"]
    assert session["store_ids"] == "263" and session["refresh_token"] == "r1"
    assert "hemmelig" not in json.dumps(session)


async def test_reads_only_and_logs_no_secrets(store, site, env_login, caplog):
    """The pickup calls are GETs: nothing is set at REMA. No password or token in the log."""
    caplog.set_level("DEBUG")
    site.api_401 = 1
    store.choose_stores([329, 263])
    await store.delivery_info()
    await store.delivery_slots(7)
    api = [r for r in site.requests if r.url.host == "api.digital.rema1000.dk"]
    assert api and all(r.method == "GET" for r in api)
    for secret in ("hemmelig", site.access, site.renewed, "r1", "s1", "4242"):
        assert secret not in caplog.text


async def test_search_never_carries_the_login_cookies(store, site, env_login):
    """The login runs on its own client (see FakeSite.api_only): a search gets none of its
    cookies, also while a login runs."""
    await asyncio.gather(store.check_login(), store.offers(1))
    await store.offers(1)
    search = [r for r in site.requests if r.url.path == "/api/search/products"]
    assert search and all("cookie" not in r.headers for r in search)


async def test_delivery_info_survives_failing_times(store, site):
    site.access, site.refresh_status = jwt(NOW - timedelta(minutes=1)), 401
    saved(site, store_ids="263")
    info = await store.delivery_info()  # an expired session: the store is still known
    assert info.pickup_store == "Aalborg C, Hasseris" and info.next_available is None
    assert "Could not read the pickup times" in info.note and "expired" in info.note
