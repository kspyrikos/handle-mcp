"""REMA 1000 login: the site's own OAuth login, then the cookies it sets. See docs/research.md.

`handle-mcp login rema1000` logs in once with email and password. Only the cookies are saved,
never the password (docs/adr/0003). The access token is a JWT of about an hour; it is renewed
with the cookies through the site's own refresh call. A queue, a bot check or an unknown host
stops the login: never worked around.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx

from handle_mcp.stores import StoreError, expired, load_login, save_session
from handle_mcp.stores._http import RETRYABLE, request

log = logging.getLogger(__name__)

API = "https://api.digital.rema1000.dk/api"
SHOP_HOST, LOGIN_HOST = "shop.rema1000.dk", "login.rema1000.dk"
APP_HOST, API_HOST = "cphapp.rema1000.dk", "api.digital.rema1000.dk"
SHOP = f"https://{SHOP_HOST}"
HOSTS = {SHOP_HOST, LOGIN_HOST, APP_HOST, API_HOST}  # the login goes nowhere else
CALLBACK = "/api/auth/callback/rema"  # where the login ends on the site: it sets the cookies
# The site's session. The refresh answers 500 without api_session and the load balancer's
# AWSALB pair, which route it to the backend that holds the session.
KEPT = ("access_token", "refresh_token", "api_session", "AWSALB", "AWSALBCORS")
NAME = "REMA 1000"
HEADERS = {
    "Accept": "application/json",
    "X-Device": "web",
    "X-Locale": "da",
    "X-Timezone": "Copenhagen/Europe",
}
MAX_HOPS = 12
MARGIN = timedelta(minutes=2)  # renew this long before the token ends
FALLBACK = timedelta(minutes=50)  # the life of a token that says nothing about its end
IDS = re.compile(r"\d+")


class Unsure(StoreError):
    """A POST whose answer was lost: it may or may not have landed. Read before sending again."""


def queue() -> StoreError:
    return StoreError("REMA 1000 currently has a queue or bot check. Try again later.")


def changed() -> StoreError:
    return StoreError("REMA 1000 redirected the login unexpectedly. Its API may have changed.")


def claims(token: str) -> dict:
    """The payload of a JWT, not checked: REMA checks it. {} when it is not a JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        data = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, binascii.Error):
        return {}
    return data if isinstance(data, dict) else {}


def number(value: Any) -> int | None:
    """A claim that is a number or a numeric string, as whole seconds (or an id). Else None."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        f = float(value)
    except ValueError:
        return None
    return int(f) if math.isfinite(f) and 0 < f < 1e12 else None


class RemaAuthMixin:
    """Login, renewal and calls with the Bearer token. Rema1000Store sets the fields."""

    name: str
    _client: httpx.AsyncClient
    _auth: httpx.AsyncClient  # the login's own client: its cookies never reach other calls
    _cookies: dict[str, str]
    _token: str | None
    _until: datetime
    _user: int | None
    _stores: str
    _auth_lock: asyncio.Lock

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _set(self, cookies: dict[str, str], saved_exp: int | None = None) -> None:
        """Keep the cookies; read the token, its end and the user id from access_token."""
        self._cookies = {k: cookies[k] for k in KEPT if cookies.get(k)}
        token = unquote(self._cookies.get("access_token", ""))
        self._token = token or None
        found = claims(token)
        if exp := number(found.get("exp")):
            self._until = datetime.fromtimestamp(exp, UTC)
        elif saved_exp:
            self._until = datetime.fromtimestamp(saved_exp, UTC)
        elif iat := number(found.get("iat")):
            self._until = datetime.fromtimestamp(iat, UTC) + FALLBACK
        else:  # never "no end": that would renew on every call
            self._until = self._now() + FALLBACK
        self._user = number(found.get("sub")) or self._user

    def _fresh(self) -> bool:
        return bool(self._token) and self._now() + MARGIN < self._until

    def session(self) -> dict[str, str] | None:
        """The cookies, when the token ends, and the chosen stores. Never the password."""
        if not self._cookies:
            return None
        out = dict(self._cookies)
        out["access_exp"] = str(int(self._until.timestamp()))
        if self._stores:
            out["store_ids"] = self._stores
        return out

    async def check_login(self) -> None:
        """For `handle-mcp login`: log in, or fail with a clear message."""
        await self._bearer()

    async def _bearer(self, force: bool = False, used: str | None = None) -> str:
        """The token to send. Logs in or renews when there is none, it is about to end, or
        `force` says the last call got a 401 with `used` (a renewal another call just made
        is not repeated)."""
        async with self._auth_lock:
            if self._token and self._fresh() and not (force and self._token == used):
                return self._token
            login = load_login(self.name)
            if not self._cookies and "session" in login:  # saved by `handle-mcp login`
                saved = login["session"]
                if not (saved.get("access_token") or saved.get("refresh_token")):
                    raise expired(self.name)
                self._stores = saved.get("store_ids", "") or self._stores
                self._set(saved, number(saved.get("access_exp")))
                if self._token and self._fresh() and not force:
                    return self._token
            elif not self._cookies:
                return await self._login(login["email"], login["password"])
            if self._cookies.get("refresh_token") and await self._renew():
                save_session(self.name, login["email"], self.session() or {})
                return self._token or ""
            if "password" in login:  # env vars: start over
                return await self._login(login["email"], login["password"])
            raise expired(self.name)

    async def _renew(self) -> bool:
        """POST /api/auth/refresh with the cookies, as the webshop does. False when REMA
        refuses. Never retried: a retry could burn a refresh token that was just rotated."""
        complete = bool(self._cookies.get("api_session") and self._cookies.get("AWSALB"))
        jar = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        headers = HEADERS | {"Origin": SHOP, "Cookie": jar}
        try:
            resp = await self._auth.post(
                f"{SHOP}/api/auth/refresh", headers=headers, follow_redirects=False
            )
        except httpx.HTTPError as exc:
            raise StoreError(f"Could not reach {NAME} ({type(exc).__name__}).") from exc
        self._auth.cookies.clear()  # the cookies live in self._cookies, nowhere else
        log.info("REMA 1000 renewal answered %s", resp.status_code)
        if resp.status_code >= 500 and complete:
            raise StoreError(
                f"REMA 1000 returned HTTP {resp.status_code} during session renewal. Try again later."
            )
        if resp.status_code >= 300:
            return False
        new = {c.name: c.value for c in resp.cookies.jar if c.name in KEPT and c.value}
        if not new.get("access_token"):
            try:
                body = resp.json()
            except ValueError:
                body = None
            tokens = body.get("tokens") if isinstance(body, dict) else None
            access = tokens.get("access_token") if isinstance(tokens, dict) else None
            if not isinstance(access, str) or not access:
                return False
            new["access_token"] = access
        self._set(self._cookies | new)
        return True

    async def _login(self, email: str, password: str) -> str:
        """The site's own login. Only the cookies of the shop's session are kept."""
        self._auth.cookies.clear()
        page = await self._follow(f"{SHOP}/konto/auth?redirect_uri=%2Fkonto", lambda u: False)
        if page.status_code != 200 or page.url.host != LOGIN_HOST:
            raise StoreError("REMA 1000: the login page could not be reached. Its API may have changed.")
        headers = HEADERS | {
            "X-Language": "da",
            "Origin": f"https://{LOGIN_HOST}",
            "Referer": str(page.url),
        }
        resp = await request(
            self._auth,
            "POST",
            f"https://{APP_HOST}/api/oauth2/login",
            NAME,
            json={"email": email, "password": password},
            headers=headers,
            follow_redirects=False,
        )
        status = resp.status_code
        if status in (400, 401, 422):
            raise StoreError("REMA 1000 rejected the login. Check your email and password.")
        if status == 403 or 300 <= status < 400:
            raise queue()
        if status >= 300:
            raise StoreError(f"REMA 1000 login failed ({status}).")
        try:
            body = resp.json()
        except ValueError:
            body = None
        nxt = body.get("redirect_uri") if isinstance(body, dict) else None
        if not isinstance(nxt, str) or not nxt:
            raise StoreError("REMA 1000 login returned no redirect path. Its API may have changed.")
        if nxt.startswith("http://"):
            nxt = "https://" + nxt.removeprefix("http://")
        # Stop once the callback has answered: it sets the cookies; the rest is the webshop.
        await self._follow(nxt, lambda u: u.host == SHOP_HOST and u.path == CALLBACK)
        cookies = {c.name: c.value for c in self._auth.cookies.jar if c.name in KEPT}
        self._auth.cookies.clear()
        if not cookies.get("access_token"):
            raise StoreError("REMA 1000 login returned no access token. Its API may have changed.")
        self._set(cookies)
        log.info("logged in to REMA 1000")
        save_session(self.name, email, self.session() or {})  # replaces an old saved password
        return self._token or ""

    async def _follow(self, start: str, stop: Callable[[httpx.URL], bool]) -> httpx.Response:
        """GET a page and follow its redirects by hand, allowed hosts only, so nothing goes
        anywhere unexpected. `stop` says the page just fetched is the last one."""
        url = start
        for _ in range(MAX_HOPS):
            try:
                u = httpx.URL(url)
            except httpx.InvalidURL as exc:
                raise changed() from exc
            host = u.host.lower()
            if u.scheme != "https" or host not in HOSTS:
                if host == "queue-it.net" or host.endswith(".queue-it.net"):
                    raise queue()
                raise changed()
            resp = await request(
                self._auth, "GET", url, NAME, headers={"Accept": "text/html"},
                follow_redirects=False,
            )  # fmt: skip
            where = resp.headers.get("location")
            if stop(u) or not 300 <= resp.status_code < 400 or not where:
                if resp.status_code >= 400:
                    raise StoreError(f"REMA 1000 login failed ({resp.status_code}).")
                return resp
            url = str(u.join(where))
        raise StoreError("REMA 1000 login entered a redirect loop. Its API may have changed.")

    async def _api(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: Any = None,
        once: bool = False,
    ) -> httpx.Response:
        """One call with the token. A 401 renews once; a second one means the session is over.
        A 403 or a redirect is a queue or a bot check: reported, never worked around. `once`
        (a POST that creates something) is never retried: a lost answer, a 429 or a 5xx raises
        Unsure, and the caller reads before it sends again (docs/adr/0007)."""
        used = None
        for attempt in range(2):
            token = await self._bearer(force=attempt > 0, used=used)
            used = token
            kwargs: dict[str, Any] = {
                "params": params,
                "json": body,
                "headers": HEADERS | {"Authorization": f"Bearer {token}"},
                "follow_redirects": False,
            }
            url = f"{API}/{path}"
            if once:
                try:
                    resp = await self._client.request(method, url, **kwargs)
                except RETRYABLE as exc:
                    raise Unsure(f"{NAME} is not responding ({type(exc).__name__}).") from exc
                except httpx.HTTPError as exc:
                    raise StoreError(f"Could not reach {NAME} ({type(exc).__name__}).") from exc
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise Unsure(f"{NAME} returned HTTP {resp.status_code}.")
            else:
                resp = await request(self._client, method, url, NAME, **kwargs)
            status = resp.status_code
            if status == 401 and attempt == 0:
                continue
            if status == 401:
                raise expired(self.name)
            if status == 403 or 300 <= status < 400:
                log.warning("rema1000 %s %s: %s", method, IDS.sub("{id}", path), status)
                raise queue()
            return resp
        raise AssertionError("unreachable")

    async def _api_json(self, path: str, what: str, params: dict | None = None) -> dict:
        resp = await self._api("GET", path, params=params)
        if resp.status_code != 200:
            raise StoreError(f"REMA 1000 rejected {what} ({resp.status_code}).")
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise StoreError(
                f"REMA 1000 returned an unexpected response ({what}). Its API may have changed."
            )
        return data
