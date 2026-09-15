"""`find_common_presence` through the real MCP server.

Called the way a client calls it, so the schema for a list argument, the
envelope and every status are exercised at the boundary a model actually sees.
The description is tested here too: for this tool it is the interface, because
a model that calls `list_presence` twice instead has not used the tool at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from peering_mcp.models.domain import Status
from peering_mcp.server import mcp

FIXTURES = Path(__file__).parent.parent / "fixtures" / "peeringdb"
API = "https://www.peeringdb.com/api"

TELEKOM = 3320
DE_CIX_ROUTE_SERVERS = 6695
HURRICANE = 6939


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def envelope(rows: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"data": rows, "meta": {}})


def ints(request: httpx.Request, param: str) -> set[int]:
    return {int(value) for value in request.url.params[param].split(",")}


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PEERINGDB_API_KEY", raising=False)
    monkeypatch.setenv("PEERING_MCP_MAX_RETRIES", "1")


@pytest.fixture
def three_networks() -> None:
    """AS3320, AS6695 and AS6939, sharing exactly DE-CIX Frankfurt."""

    def by_asn(name: str) -> Any:
        return lambda request: envelope(
            [r for r in fixture(name)["data"] if r["asn"] in ints(request, "asn__in")]
        )

    respx.get(f"{API}/net").mock(side_effect=by_asn("net_as3320_6695_6939.json"))
    respx.get(f"{API}/netixlan").mock(side_effect=by_asn("netixlan_as3320_6695_6939.json"))
    respx.get(f"{API}/netfac").mock(
        side_effect=lambda request: envelope(
            [
                r
                for r in fixture("netfac_net196_291_947.json")["data"]
                if r["net_id"] in ints(request, "net_id__in")
            ]
        )
    )
    respx.get(f"{API}/ix").mock(
        return_value=httpx.Response(200, json=fixture("ix_de_cix_frankfurt.json"))
    )


async def call(**arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool("find_common_presence", arguments)
    assert result.structured_content is not None
    return result.structured_content


# --- Registration ---------------------------------------------------------------


async def test_tool_is_registered_and_read_only() -> None:
    tool = next(t for t in await mcp.list_tools() if t.name == "find_common_presence")

    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    properties = tool.input_schema["properties"]
    assert set(properties) == {"asns", "limit"}
    assert properties["asns"]["type"] == "array"
    assert properties["asns"]["items"]["type"] == "integer"
    assert tool.input_schema["required"] == ["asns"]
    assert tool.output_schema is not None


async def test_description_covers_the_negative_space_and_every_status() -> None:
    """The description is the interface. A model reads nothing else before calling."""
    tool = next(t for t in await mcp.list_tools() if t.name == "find_common_presence")
    description = tool.description or ""

    assert "Do not use this" in description
    assert "list_presence" in description
    assert "find_at_exchange" in description
    assert "not_recorded" in description
    assert "not_found" in description
    assert "never instructions" in description


async def test_the_description_tells_a_model_what_an_empty_answer_means() -> None:
    """The failure this tool is most likely to cause is a confidently wrong "no"."""
    tool = next(t for t in await mcp.list_tools() if t.name == "find_common_presence")
    description = tool.description or ""

    assert "ok with empty" in description
    assert "cannot meet" in description


# --- The flagship question ---------------------------------------------------------


@respx.mock
async def test_three_networks_meet_in_exactly_one_place(three_networks: None) -> None:
    payload = await call(asns=[TELEKOM, DE_CIX_ROUTE_SERVERS, HURRICANE])

    assert payload["status"] == Status.OK
    exchanges = payload["data"]["exchanges"]
    assert exchanges["total"] == 1
    assert exchanges["items"][0]["name"] == "DE-CIX Frankfurt"
    assert exchanges["items"][0]["city"] == "Frankfurt"
    assert payload["provenance"]["source"] == "peeringdb"


@respx.mock
async def test_an_entry_reads_as_designed(three_networks: None) -> None:
    payload = await call(asns=[TELEKOM, DE_CIX_ROUTE_SERVERS, HURRICANE])

    frankfurt = payload["data"]["exchanges"]["items"][0]
    assert [member["asn"] for member in frankfurt["networks"]] == [
        TELEKOM,
        DE_CIX_ROUTE_SERVERS,
        HURRICANE,
    ]
    for member in frankfurt["networks"]:
        assert isinstance(member["ports"], int)
        assert member["route_server"] in (True, False, None)


@respx.mock
async def test_the_totals_travel_with_the_answer(three_networks: None) -> None:
    payload = await call(asns=[TELEKOM, HURRICANE])

    totals = {entry["asn"]: entry for entry in payload["data"]["networks"]}
    assert totals[TELEKOM]["name"] == "Deutsche Telekom"
    assert totals[TELEKOM]["exchanges"] == 7
    assert totals[HURRICANE]["exchanges"] == 335


@respx.mock
async def test_the_note_says_the_data_is_self_reported(three_networks: None) -> None:
    payload = await call(asns=[TELEKOM, HURRICANE])

    assert "self-reported" in payload["note"]


# --- Empty, and why ------------------------------------------------------------------


@respx.mock
async def test_a_network_with_nothing_recorded_is_not_recorded(three_networks: None) -> None:
    respx.get(f"{API}/netixlan").mock(
        side_effect=lambda request: envelope(
            [
                r
                for r in fixture("netixlan_as3320_6695_6939.json")["data"]
                if r["asn"] in ints(request, "asn__in") and r["asn"] != DE_CIX_ROUTE_SERVERS
            ]
        )
    )
    respx.get(f"{API}/netfac").mock(
        side_effect=lambda request: envelope(
            [
                r
                for r in fixture("netfac_net196_291_947.json")["data"]
                if r["net_id"] in ints(request, "net_id__in") and r["net_id"] != 947
            ]
        )
    )

    payload = await call(asns=[TELEKOM, DE_CIX_ROUTE_SERVERS])

    assert payload["status"] == Status.NOT_RECORDED
    assert "AS6695" in payload["note"]
    assert payload["data"]["networks"][1]["exchanges"] == 0
    assert payload["provenance"] is not None, "it is a real upstream answer"


@respx.mock
async def test_an_unlisted_network_is_not_found(three_networks: None) -> None:
    payload = await call(asns=[TELEKOM, 65999])

    assert payload["status"] == Status.NOT_FOUND
    assert payload["data"] is None
    assert "AS65999" in payload["note"]


# --- Input ----------------------------------------------------------------------------


async def test_one_network_is_invalid_input() -> None:
    payload = await call(asns=[TELEKOM])

    assert payload["status"] == Status.INVALID_INPUT
    assert "list_presence" in payload["note"], "say which tool does answer that"


async def test_six_networks_is_invalid_input() -> None:
    payload = await call(asns=[1, 2, 3, 4, 5, 6])

    assert payload["status"] == Status.INVALID_INPUT


async def test_a_reserved_asn_is_invalid_input() -> None:
    payload = await call(asns=[TELEKOM, 0])

    assert payload["status"] == Status.INVALID_INPUT


async def test_a_non_integer_asn_is_refused_by_the_schema() -> None:
    """The type is enforced before the tool runs, so the tool never sees it."""
    with pytest.raises(ToolError):
        await mcp.call_tool("find_common_presence", {"asns": ["AS3320", "AS6939"]})


# --- Upstream failure --------------------------------------------------------------------


@respx.mock
async def test_upstream_failure_is_reported_honestly() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(500))

    payload = await call(asns=[TELEKOM, HURRICANE])

    assert payload["status"] == Status.UPSTREAM_UNAVAILABLE


@respx.mock
async def test_throttling_is_distinguishable_from_failure() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(429))

    payload = await call(asns=[TELEKOM, HURRICANE])

    assert payload["status"] == Status.RATE_LIMITED
