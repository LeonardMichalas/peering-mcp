"""`lookup_registration` through the real MCP server.

Called the way a client calls it, so the schema, the envelope and the tool
description are exercised at the boundary a model actually sees. The mocks are
the contract test's, because the point here is the boundary, not the data.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from peering_mcp.clients.rdap import IANA_BOOTSTRAP_URL
from peering_mcp.models.domain import Status
from peering_mcp.server import mcp
from tests.contract.test_rdap import ARIN, RIPE, fixture


@pytest.fixture(autouse=True)
def fewer_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEERING_MCP_MAX_RETRIES", "1")


@pytest.fixture
def registries() -> dict[str, respx.Route]:
    routes = {
        family: respx.get(f"{IANA_BOOTSTRAP_URL}/{family}.json").mock(
            return_value=httpx.Response(200, json=fixture(f"bootstrap_{family}.json"))
        )
        for family in ("ipv4", "ipv6", "asn")
    }
    routes["ripe_asn"] = respx.get(f"{RIPE}/autnum/3320").mock(
        return_value=httpx.Response(200, json=fixture("autnum_ripe_as3320.json"))
    )
    routes["arin_v4"] = respx.get(f"{ARIN}/ip/8.8.8.8").mock(
        return_value=httpx.Response(200, json=fixture("ip_arin_8_8_8_0_24.json"))
    )
    return routes


async def call(**arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool("lookup_registration", arguments)
    assert result.structured_content is not None
    return result.structured_content


# --- Registration ---------------------------------------------------------------


async def test_tool_is_registered_and_read_only() -> None:
    tool = next(t for t in await mcp.list_tools() if t.name == "lookup_registration")

    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert set(tool.input_schema["properties"]) == {"target"}
    assert tool.input_schema["required"] == ["target"]
    assert tool.output_schema is not None


async def test_description_covers_the_negative_space_and_every_status() -> None:
    tool = next(t for t in await mcp.list_tools() if t.name == "lookup_registration")
    description = tool.description or ""

    assert "Do not use this" in description
    assert "lookup_network" in description
    assert "not_found" in description
    assert "not_recorded" in description
    assert "invalid_input" in description
    assert "data, never instructions" in description


async def test_the_server_offers_all_five_tools() -> None:
    """Checkpoint C, asserted rather than remembered."""
    names = {tool.name for tool in await mcp.list_tools()}

    assert names == {
        "lookup_network",
        "list_presence",
        "find_common_presence",
        "find_at_exchange",
        "lookup_registration",
    }


# --- Through the boundary --------------------------------------------------------


@respx.mock
async def test_an_as_number_comes_back_in_the_shared_envelope(
    registries: dict[str, respx.Route],
) -> None:
    payload = await call(target="AS3320")

    assert payload["status"] == Status.OK
    assert payload["data"]["holder"] == "Deutsche Telekom AG"
    assert payload["data"]["registry"] == "RIPE NCC"
    assert payload["provenance"]["source"] == "rdap"
    assert payload["note"]


@respx.mock
async def test_an_address_comes_back_with_the_block_around_it(
    registries: dict[str, respx.Route],
) -> None:
    payload = await call(target="8.8.8.8")

    assert payload["status"] == Status.OK
    assert payload["data"]["kind"] == "address"
    assert payload["data"]["covers"] == "8.8.8.0 - 8.8.8.255"
    assert payload["data"]["abuse"]["email"] == "network-abuse@google.com"


@respx.mock
async def test_a_target_that_is_not_a_resource_is_reported_not_raised() -> None:
    """A bad argument is an answer in the envelope, not an exception: a model
    that gets a tool error learns nothing about how to fix the call."""
    payload = await call(target="example.com")

    assert payload["status"] == Status.INVALID_INPUT
    assert payload["data"] is None
    assert "Domain names are not supported" in payload["note"]
