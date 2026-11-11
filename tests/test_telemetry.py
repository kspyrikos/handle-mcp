"""Anonymous usage counts: what is sent, when, and that it can never break a tool."""

import json
import threading
from datetime import date

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from handle_mcp import server, telemetry
from handle_mcp import stores as registry
from tests.conftest import LEAK


@pytest.fixture
def sent(monkeypatch):
    """Telemetry on, with a website id, and a sender that records instead of posting."""
    events: list[dict] = []
    monkeypatch.delenv("HANDLE_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.setenv("HANDLE_TELEMETRY_WEBSITE", "site-1")
    monkeypatch.setattr(telemetry, "_post", events.append)
    monkeypatch.setattr(telemetry, "QUIET", 3600)  # tests flush by hand

    class Now(threading.Thread):  # run the "background" send at once
        def start(self):
            self.run()

    monkeypatch.setattr(telemetry, "Thread", Now)
    return events


def names(events):
    return [e["payload"]["name"] for e in events]


async def fill(store="fake", items=None):
    await server.set_cart_quantities(store, items or {"2": 3})
    telemetry._flush_all()


async def test_a_filled_cart_sends_exactly_the_documented_fields(sent):
    await fill()
    event = sent[-1]["payload"]
    assert event["name"] == "cart_filled" and event["website"] == "site-1"
    assert event["url"] == "/cart_filled/fake"  # the weekly script filters stores by path
    # FakeStore: "1" x2 + "2" x3 at 12.95 + 39 fee = 103.75 kr: rounds to 100
    assert event["data"] == {
        "store": "fake",
        "basket_total_dkk": 100,
        "item_count": 5,
        "version": telemetry._version(),
        "date": date.today().isoformat(),
    }


async def test_no_personal_data_and_no_products_leave(sent):
    await fill()
    text = json.dumps(sent).lower()
    for value in [*LEAK.values(), "Andeby 1", "mælk", "Mælk"]:
        assert str(value).lower() not in text
    assert "product" not in text and "order" not in text


async def test_many_changes_count_as_one_cart(sent):
    for qty in (1, 2, 3):
        await server.set_cart_quantities("fake", {"2": qty})
    telemetry._flush_all()
    assert names(sent).count("cart_filled") == 1
    assert sent[-1]["payload"]["data"]["item_count"] == 5


def test_a_cart_counts_after_30_quiet_minutes():
    assert telemetry.QUIET == 1800


async def test_edits_after_the_count_send_nothing(sent):
    await fill()
    await fill(items={"2": 5})  # the human changes the cart the next day
    telemetry._pending.clear()  # and after a restart: the lock is in the file
    await fill(items={"1": 4})
    assert names(sent).count("cart_filled") == 1


async def test_an_empty_cart_unlocks_so_the_next_order_counts(sent):
    await fill()
    await server.set_cart_quantities("fake", {"1": 0, "2": 0})  # checkout empties the cart
    await server.get_cart("fake")
    await fill(items={"2": 2})
    assert names(sent).count("cart_filled") == 2


async def test_reading_an_empty_cart_unlocks(sent):
    await fill()
    server._store("fake").cart.clear()  # the order went through on the store's site
    await server.get_cart("fake")
    await fill()
    assert names(sent).count("cart_filled") == 2


async def test_an_order_after_checkout_counts_without_a_read_first(sent):
    await fill()
    server._store("fake").cart.clear()  # paid on the store's site; no tool read the cart
    await fill()  # the model fills next week's cart straight away
    assert names(sent).count("cart_filled") == 2


async def test_a_cart_paid_before_30_minutes_still_counts(sent):
    await server.set_cart_quantities("fake", {"2": 3})
    server._store("fake").cart.clear()  # paid at once
    await server.get_cart("fake")
    assert names(sent).count("cart_filled") == 1
    await fill()
    assert names(sent).count("cart_filled") == 2


async def test_the_daily_file_write_and_the_lock_do_not_lose_each_other(sent):
    await fill()
    telemetry.start()
    state = json.loads((registry.CONFIG_DIR / "telemetry.json").read_text())
    assert state["counted"] == ["fake"] and "server_started" in state["days"]


async def test_off_means_an_empty_cart_read_touches_no_file(sent, monkeypatch):
    monkeypatch.setenv("HANDLE_TELEMETRY", "0")
    server._store("fake").cart.clear()
    await server.get_cart("fake")
    assert not (registry.CONFIG_DIR / "telemetry.json").exists()


async def test_a_failing_pre_read_never_blocks_the_write(sent, monkeypatch):
    await fill()
    store = server._store("fake")

    async def down():
        raise registry.StoreError("nemlig.com har kø lige nu.")

    monkeypatch.setattr(store, "get_cart", down)
    with pytest.raises(ToolError):  # the read after the write fails too, as it should
        await server.set_cart_quantities("fake", {"2": 9})
    assert store.cart["2"] == 9  # but the write went through


async def test_reading_a_full_cart_keeps_the_lock(sent):
    await fill()
    await server.get_cart("fake")
    await fill(items={"2": 7})
    assert names(sent).count("cart_filled") == 1


def test_the_lock_keeps_store_names_only(sent):
    telemetry.cart_filled("bilkatogo", {"total": "100", "lines": [{"quantity": 1}]}, [])
    telemetry._flush_all()
    state = json.loads((registry.CONFIG_DIR / "telemetry.json").read_text())
    assert state == {"counted": ["bilkatogo"]}


async def test_an_emptied_cart_is_not_counted(sent):
    await server.set_cart_quantities("fake", {"2": 2})
    await server.set_cart_quantities("fake", {"1": 0, "2": 0})
    telemetry._flush_all()
    assert "cart_filled" not in names(sent)


async def test_a_cart_with_a_failed_line_is_not_counted(sent, monkeypatch):
    from handle_mcp.models import QuantityResult

    async def refuse(self, items):
        return [QuantityResult(pid, q, 0, False) for pid, q in items.items()]

    monkeypatch.setattr(registry.STORES["fake"], "set_quantities", refuse)
    await fill()
    assert "cart_filled" not in names(sent)


async def test_bilkatogo_is_counted_as_bilka(sent):
    telemetry.cart_filled("bilkatogo", {"total": "624.10", "lines": [{"quantity": 7}]}, [])
    telemetry._flush_all()
    assert sent[-1]["payload"]["data"]["store"] == "bilka"
    assert sent[-1]["payload"]["data"]["basket_total_dkk"] == 600


@pytest.mark.parametrize(
    ("total", "rounded"), [("0", 0), ("24.99", 0), ("25.00", 50), ("649.50", 650), ("675", 700)]
)
def test_totals_round_to_the_nearest_50(total, rounded):
    assert telemetry.basket({"total": total, "lines": []})[0] == rounded


def test_server_started_once_per_day_with_version_and_os(sent, monkeypatch):
    monkeypatch.setattr(telemetry.platform, "system", lambda: "Darwin")
    telemetry.start()
    monkeypatch.setattr(telemetry, "_sent_today", {})  # a restart, same day
    telemetry.start()
    assert names(sent) == ["server_started"]
    assert sent[0]["payload"]["data"] == {
        "os": "darwin",
        "version": telemetry._version(),
        "date": date.today().isoformat(),
    }


async def test_first_tool_call_once_per_day_with_the_tool_name(sent, monkeypatch):
    await server.list_offers()
    await server.get_cart("fake")
    monkeypatch.setattr(telemetry, "_sent_today", {})  # a restart, same day
    await server.search_products("mælk")
    calls = [e["payload"]["data"] for e in sent if e["payload"]["name"] == "first_tool_call"]
    assert [c["tool"] for c in calls] == ["list_offers"]


async def test_listing_the_tools_is_not_a_tool_call(sent):
    await server.mcp.list_tools()
    assert sent == []


async def test_login_ok_once_per_store_per_day(sent, monkeypatch):
    await server.get_cart("fake")
    await server.set_cart_quantities("fake", {"2": 1})
    monkeypatch.setattr(telemetry, "_sent_today", {})  # a restart, same day
    await server.get_cart()
    logins = [e["payload"] for e in sent if e["payload"]["name"] == "login_ok"]
    assert [e["data"]["store"] for e in logins] == ["fake", "broken"]
    assert logins[0]["url"] == "/login_ok/fake"


async def test_no_login_no_login_ok(sent, monkeypatch):
    from handle_mcp import stores as registry

    async def no_login(self):
        raise registry.StoreError("Ikke logget ind.")

    monkeypatch.setattr(registry.STORES["fake"], "get_cart", no_login)
    with pytest.raises(ToolError):
        await server.get_cart("fake")
    assert "login_ok" not in names(sent)


def test_the_file_keeps_days_never_an_id(sent):
    telemetry.start()
    telemetry.login_ok("bilkatogo")
    telemetry.notice()
    state = json.loads((registry.CONFIG_DIR / "telemetry.json").read_text())
    today = date.today().isoformat()
    assert state == {
        "notice_shown": True,
        "days": {"server_started": today, "login_ok:bilka": today},
    }


@pytest.mark.parametrize(
    ("env", "value"),
    [("HANDLE_TELEMETRY", "0"), ("HANDLE_TELEMETRY", "off"), ("DO_NOT_TRACK", "1")],
)
async def test_env_turns_it_off(sent, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    telemetry.start()
    await fill()
    assert sent == []
    assert not (registry.CONFIG_DIR / "telemetry.json").exists()


def test_flag_turns_it_off(sent, monkeypatch):
    monkeypatch.setattr("sys.argv", ["handle-mcp", "--no-telemetry"])
    monkeypatch.setattr(server.mcp, "run", lambda transport: None)
    server.main()
    assert not telemetry.enabled() and sent == []


def test_flag_works_before_a_cli_command(monkeypatch):
    calls = []
    monkeypatch.setattr("sys.argv", ["handle-mcp", "--no-telemetry", "logout", "fake"])
    monkeypatch.setattr("handle_mcp.cli.run", lambda args: calls.append(args) or 0)
    with pytest.raises(SystemExit):
        server.main()
    assert calls == [["logout", "fake"]]


def test_the_notice_goes_to_stderr_never_stdout(sent, capsys):
    telemetry.start()
    out, err = capsys.readouterr()
    assert out == ""  # stdout is JSON-RPC
    assert "HANDLE_TELEMETRY=0" in err and "--no-telemetry" in err


async def test_the_first_answer_tells_the_human_once(sent, monkeypatch):
    first = await server.get_cart("fake")
    assert "HANDLE_TELEMETRY=0" in first["telemetry_notice"]
    assert "telemetry_notice" not in await server.get_cart("fake")
    monkeypatch.setattr(telemetry, "_noticed", False)  # a restart: the file remembers
    assert "telemetry_notice" not in await server.get_cart("fake")


async def test_no_notice_when_it_is_off(sent, monkeypatch):
    monkeypatch.setenv("HANDLE_TELEMETRY", "0")
    assert "telemetry_notice" not in await server.get_cart("fake")


async def test_the_model_is_told_to_pass_the_notice_on():
    assert "telemetry_notice" in server.mcp.instructions


def test_instructions_suggest_a_first_prompt():
    text = server.mcp.instructions
    assert "Plan this week's meals" in text
    assert "at most 3 short questions" in text
    assert "including this week" in text
    assert "minimum of 1 week" in text


async def test_a_failing_endpoint_never_breaks_a_tool(sent, monkeypatch):
    def boom(payload):
        raise OSError("network down")

    monkeypatch.setattr(telemetry, "_send", lambda *a, **k: boom(a))
    monkeypatch.setattr(telemetry, "_state_file", lambda: boom(None))
    result = await server.set_cart_quantities("fake", {"2": 1})
    telemetry._flush_all()
    assert result["results"][0]["ok"]


def test_post_swallows_network_errors(monkeypatch):
    def down(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(telemetry.httpx, "post", down)
    telemetry._post({"type": "event"})  # does not raise


def test_post_has_a_short_timeout_and_a_configurable_url(monkeypatch):
    seen = {}
    monkeypatch.setattr(telemetry.httpx, "post", lambda url, **kw: seen.update(url=url, **kw))
    monkeypatch.setenv("HANDLE_TELEMETRY_URL", "https://umami.example.com/api/send")
    telemetry._post({"type": "event"})
    assert seen["url"] == "https://umami.example.com/api/send"
    assert seen["timeout"] <= 2


def test_no_website_id_sends_nothing(sent, monkeypatch):
    monkeypatch.delenv("HANDLE_TELEMETRY_WEBSITE")
    monkeypatch.setattr(telemetry, "WEBSITE", "")
    telemetry._send("server_started", {})
    assert sent == []
