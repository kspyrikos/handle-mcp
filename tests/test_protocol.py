"""The tools as an MCP client sees them: real JSON-RPC (initialize handshake) in-process.

The tool descriptions are prompts for the model. tests/snapshots/tools.json holds names,
descriptions, input schemas and annotations, so every change to them shows in the diff.
Update on purpose: HANDLE_UPDATE_SNAPSHOT=1 uv run pytest tests/test_protocol.py
"""

import inspect
import json
import os
from pathlib import Path

import pytest
from mcp import Client

from handle_mcp import server
from tests.conftest import LEAK
from tests.test_privacy import CALLS

SNAPSHOT = Path(__file__).parent / "snapshots" / "tools.json"


async def _client_tools() -> list[dict]:
    async with Client(server.mcp, mode="legacy") as client:
        tools = (await client.list_tools()).tools
    return [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools]


async def test_tool_surface_matches_the_snapshot():
    tools = sorted(await _client_tools(), key=lambda t: t["name"])
    for tool in tools:  # Python 3.13+ strips docstring indents itself; 3.12 does not
        tool["description"] = inspect.cleandoc(tool["description"])
    if os.environ.get("HANDLE_UPDATE_SNAPSHOT") == "1":
        SNAPSHOT.write_text(
            json.dumps(tools, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        pytest.skip("snapshot written: check the diff")
    assert tools == json.loads(SNAPSHOT.read_text(encoding="utf-8")), (
        "A tool's name, description, arguments or annotations changed. If on purpose: "
        "HANDLE_UPDATE_SNAPSHOT=1 uv run pytest tests/test_protocol.py"
    )


async def test_write_tools_are_marked_and_never_order_or_pay():
    writes = {}
    for tool in await _client_tools():
        if not tool["annotations"]["readOnlyHint"]:
            assert tool["annotations"]["destructiveHint"] is True, tool["name"]
            assert "Never orders or pays." in tool["description"], tool["name"]
            writes[tool["name"]] = tool["description"]
    assert set(writes) == {"set_cart_quantities", "add_recipe_to_cart"}
    assert "ASK THE HUMAN FIRST" in writes["add_recipe_to_cart"]  # it adds a whole recipe


@pytest.mark.parametrize(("tool", "args"), CALLS, ids=[c[0] for c in CALLS])
async def test_every_tool_answers_json_without_personal_data(tool, args):
    async with Client(server.mcp, mode="legacy") as client:
        result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    text = "".join(c.text for c in result.content if c.type == "text")
    json.loads(text)  # the model gets JSON it can read
    for secret in LEAK.values():
        assert str(secret) not in text, (tool, secret)


@pytest.mark.parametrize(
    ("tool", "args", "text"),
    [
        ("get_cart", {"store": "nope"}, "Unknown store 'nope'. Known stores: broken, fake."),
        (
            "set_cart_quantities",
            {"store": "fake", "items": {"1": 99}},
            "Quantity must be between 0 and 50. Invalid for: 1.",
        ),
        (
            "set_cart_quantities",
            {"store": "fake", "items": {"1": "mange"}},
            "Quantity must be a whole number",
        ),
        (
            "set_cart_quantities",
            {"store": "fake", "items": {"1": "2_0"}},  # int() reads 20
            "Quantity must be a whole number",
        ),
        (
            "set_cart_quantities",
            {"store": "fake", "items": {"1": True}},  # int() reads 1
            "Quantity must be a whole number",
        ),
        (
            "add_recipe_to_cart",
            {"store": "fake", "recipe": "r1", "skip": ["Pizza"]},
            "The recipe does not contain 'Pizza'. Ingredients: mælk.",
        ),
    ],
)
async def test_errors_reach_the_model_as_clear_text(tool, args, text):
    async with Client(server.mcp, mode="legacy") as client:
        result = await client.call_tool(tool, args)
    assert result.is_error
    assert text in "".join(c.text for c in result.content if c.type == "text")


async def test_one_failing_store_is_an_answer_not_a_tool_error():
    async with Client(server.mcp, mode="legacy") as client:
        result = await client.call_tool("search_products", {"query": "mælk", "stores": ["broken"]})
    assert not result.is_error
    text = "".join(c.text for c in result.content if c.type == "text")
    assert json.loads(text) == {"broken": {"error": "broken: API'et svarer 500"}}


async def test_every_tool_is_called_above():
    assert {t["name"] for t in await _client_tools()} == {tool for tool, _ in CALLS}
