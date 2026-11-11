"""The shared retry loop: only timeouts, network errors, 429 and 5xx are retried."""

import httpx
import pytest

from handle_mcp.stores import StoreError, _http


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(_http.asyncio, "sleep", fake_sleep)
    return waits


def client(answers):
    """Answers in order; the last one repeats."""
    calls = []

    def handle(request):
        calls.append(request)
        answer = answers[min(len(calls) - 1, len(answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), calls


async def test_retries_5xx_and_429_with_backoff(no_sleep):
    c, calls = client([503, 429, 200])
    resp = await _http.request(c, "GET", "https://x.test/a", "Butik")
    assert resp.status_code == 200
    assert len(calls) == 3
    assert no_sleep == [0.5, 1.0]


async def test_gives_up_after_retries():
    c, calls = client([503])
    with pytest.raises(StoreError, match="Butik returned HTTP 503"):
        await _http.request(c, "GET", "https://x.test/a", "Butik")
    assert len(calls) == _http.RETRIES + 1


async def test_4xx_is_not_retried():
    c, calls = client([404])
    resp = await _http.request(c, "GET", "https://x.test/a", "Butik")
    assert resp.status_code == 404
    assert len(calls) == 1


async def test_timeout_and_network_errors_are_retried_then_clear():
    c, calls = client([httpx.ReadTimeout("slow"), httpx.ReadError("gone"), 200])
    resp = await _http.request(c, "GET", "https://x.test/a", "Butik")
    assert resp.status_code == 200
    assert len(calls) == 3

    c, calls = client([httpx.ConnectError("down")])
    with pytest.raises(StoreError, match="Butik is not responding \\(ConnectError\\)"):
        await _http.request(c, "GET", "https://x.test/a", "Butik")
    assert len(calls) == _http.RETRIES + 1


async def test_other_http_errors_are_not_retried():
    c, calls = client([httpx.TooManyRedirects("loop")])
    with pytest.raises(StoreError, match="Could not reach Butik"):
        await _http.request(c, "GET", "https://x.test/a", "Butik")
    assert len(calls) == 1
