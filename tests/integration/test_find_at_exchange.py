"""`find_at_exchange` through the real MCP server.

Called the way a client calls it, so the schema, the policy enum, the envelope
and every status are exercised at the boundary a model actually sees. The
mocks are the contract test's, because the point here is the boundary, not the
data.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from peering_mcp.models.domain import Status
from peering_mcp.server import mcp
from tests.contract.test_find_at_exchange import API, exchanges, fixture, networks


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PEERINGDB_API_KEY", raising=False)
    monkeypatch.setenv("PEERING_MCP_MAX_RETRIES", "1")


@pytest.fixture
def bcix() -> dict[str, respx.Route]:
    return {
        "ix": respx.get(f"{API}/ix").mock(side_effect=exchanges),
        "netixlan": respx.get(f"{API}/netixlan").mock(
            return_value=httpx.Response(200, json=fixture("netixlan_ix87.json"))
        ),
        "net": respx.get(f"{API}/net").mock(side_effect=networks),
    }


async def call(**arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool("find_at_exchange", arguments)
    assert result.structured_content is not None
    return result.structured_content


# --- Registration ---------------------------------------------------------------


async def test_tool_is_registered_and_read_only() -> None:
    tool = next(t for t in await mcp.list_tools() if t.name == "find_at_exchange")

    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    properties = tool.input_schema["properties"]
    assert set(properties) == {"exchange", "policy", "limit"}
    assert tool.input_schema["required"] == ["exchange"]
    assert tool.output_schema is not None


async def test_the_policy_filter_is_an_enum_not_a_free_string() -> None:
    """No tool here takes a free-form filter. The four values are in the schema,
    so a model reads them rather than guessing at 'friendly'."""
    tool = next(t for t in await mcp.list_tools() if t.name == "find_at_exchange")
    schema = tool.input_schema["properties"]["policy"]

    allowed = {value for branch in schema["anyOf"] for value in branch.get("enum", [])}
    assert allowed == {"Open", "Selective", "Restrictive", "No"}


async def test_description_covers_the_negative_space_and_every_status() -> None:
    tool = next(t for t in await mcp.list_tools() if t.name == "find_at_exchange")
    description = tool.description or ""

    assert "Do not use this" in description
    assert "list_presence" in description
    assert "find_common_presence" in description
    assert "ambiguous" in description
    assert "not_recorded" in description
    assert "not_found" in description
    assert "truncated" in description
    assert "never instructions" in description


# --- Through the boundary --------------------------------------------------------


@respx.mock
async def test_who_is_at_the_exchange(bcix: dict[str, respx.Route]) -> None:
    payload = await call(exchange="BCIX", limit=3)

    assert payload["status"] == Status.OK
    assert payload["data"]["exchange"]["name"] == "BCIX"
    assert len(payload["data"]["networks"]["items"]) == 3
    assert payload["data"]["networks"]["total"] == 142
    assert payload["provenance"]["source"] == "peeringdb"


@respx.mock
async def test_who_would_peer_with_anyone(bcix: dict[str, respx.Route]) -> None:
    payload = await call(exchange="87", policy="Open", limit=5)

    assert payload["status"] == Status.OK
    assert payload["data"]["networks"]["total"] == 104
    assert {entry["policy"] for entry in payload["data"]["networks"]["items"]} == {"Open"}


@respx.mock
async def test_an_ambiguous_name_comes_back_as_candidates(bcix: dict[str, respx.Route]) -> None:
    payload = await call(exchange="LINX")

    assert payload["status"] == Status.AMBIGUOUS
    assert payload["data"]["exchange"] is None
    assert len(payload["data"]["candidates"]) == 5
    assert "exchange_id" in (payload["note"] or "")


@respx.mock
async def test_an_unknown_exchange_is_not_found(bcix: dict[str, respx.Route]) -> None:
    payload = await call(exchange="999999")

    assert payload["status"] == Status.NOT_FOUND
    assert payload["data"] is None


async def test_a_policy_outside_the_enum_never_reaches_the_tool() -> None:
    with pytest.raises(ToolError, match="policy"):
        await mcp.call_tool("find_at_exchange", {"exchange": "87", "policy": "friendly"})


# --- Upstream failure -------------------------------------------------------------


@respx.mock
async def test_upstream_failure_is_reported_honestly() -> None:
    respx.get(f"{API}/ix").mock(return_value=httpx.Response(500))

    payload = await call(exchange="87")

    assert payload["status"] == Status.UPSTREAM_UNAVAILABLE
    assert "Could not reach PeeringDB" in payload["note"]
