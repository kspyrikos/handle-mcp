"""One HTTP request with the retries every store needs, and per_product() for stores that
need one request per product.

Retry only on timeout, network errors, 429 and 5xx. Safe because every write sets an
absolute quantity. Anything else is the store's own answer and goes back to the caller.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from handle_mcp.models import ProductNutrition
from handle_mcp.stores import NotFound, StoreError

RETRIES = 2  # extra attempts
RETRYABLE = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
AT_ONCE = 5  # per_product: calls in flight to one store, kind and quick enough


async def request(
    client: httpx.AsyncClient, method: str, url: str, store: str, **kwargs: Any
) -> httpx.Response:
    """Send the request. `store` is the name in error messages, e.g. "nemlig.com"."""
    for attempt in range(RETRIES + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except RETRYABLE as exc:
            if attempt == RETRIES:
                raise StoreError(f"{store} is not responding ({type(exc).__name__}).") from exc
        except httpx.HTTPError as exc:
            raise StoreError(f"Could not reach {store} ({type(exc).__name__}).") from exc
        else:
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            if attempt == RETRIES:
                raise StoreError(f"{store} returned HTTP {resp.status_code}. Try again later.")
        await asyncio.sleep(0.5 * 2**attempt)
    raise AssertionError("unreachable")


async def per_product(
    store: str, ids: list[str], one: Callable[[str], Awaitable[ProductNutrition]], unknown: str
) -> list[ProductNutrition]:
    """`one(id)` for every id, AT_ONCE at a time, for a store with one call per product.
    One failing product never hides the others: it gets its error as `reason` (NotFound:
    `unknown`). Every product failing (a queue, a format change) raises the first error."""
    gate = asyncio.Semaphore(AT_ONCE)

    async def guarded(pid: str) -> ProductNutrition:
        async with gate:
            return await one(pid)

    results = await asyncio.gather(*(guarded(i) for i in ids), return_exceptions=True)
    for r in results:  # anything but a store error is a bug: let the server log it
        if isinstance(r, BaseException) and not isinstance(r, StoreError):
            raise r
    if all(isinstance(r, StoreError) and not isinstance(r, NotFound) for r in results):
        raise results[0]
    return [
        ProductNutrition(store, pid, None, None, unknown if isinstance(r, NotFound) else str(r))
        if isinstance(r, StoreError)
        else r
        for pid, r in zip(ids, results, strict=True)
    ]
