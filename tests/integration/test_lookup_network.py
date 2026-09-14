"""End-to-end tests through the real MCP server.

Tools are called the way a client calls them, through `mcp.call_tool`, so the
schema, the envelope and the error mapping are all exercised. respx stands in
for PeeringDB; nothing here touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from peering_mcp.models.domain import Status
from peering_mcp.server import mcp

FIXTURES = Path(__file__).parent.parent / "fixtures" / "peeringdb"
API = "https://www.peeringdb.com/api"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not depend on whether a developer has a key exported."""
    monkeypatch.delenv("PEERINGDB_API_KEY", raising=False)
    monkeypatch.setenv("PEERING_MCP_MAX_RETRIES", "1")


async def call(query: str) -> dict:
    result = await mcp.call_tool("lookup_network", {"query": query})
    assert result.structured_content is not None, "the tool should return structured output"
    return result.structured_content


# --- The tool is registered correctly --------------------------------------


async def test_tool_is_registered_and_read_only() -> None:
    tools = await mcp.list_tools()
    names = {t.name: t for t in tools}

    assert "lookup_network" in names
    tool = names["lookup_network"]
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert "query" in tool.input_schema["properties"]
    assert tool.output_schema is not None


async def test_description_tells_the_model_when_not_to_use_it() -> None:
    """Half of a good tool description is the negative space."""
    tool = next(t for t in await mcp.list_tools() if t.name == "lookup_network")
    description = tool.description or ""

    assert "Do not use this" in description
    assert "find_common_presence" in description
    assert "not_found" in description
    assert "ambiguous" in description, "a status the model can get must be documented"


# --- The happy paths -------------------------------------------------------


@respx.mock
async def test_lookup_by_asn() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=fixture("net_as3320.json")))

    payload = await call("AS3320")

    assert payload["status"] == Status.OK
    network = payload["data"]["network"]
    assert network["asn"] == 3320
    assert network["name"] == "Deutsche Telekom"
    assert network["policy"]["general"] == "Restrictive"


@respx.mock
async def test_bare_number_works_too() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=fixture("net_as3320.json")))

    payload = await call("3320")

    assert payload["data"]["network"]["asn"] == 3320


@respx.mock
async def test_every_answer_carries_provenance() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=fixture("net_as3320.json")))

    payload = await call("AS3320")

    provenance = payload["provenance"]
    assert provenance["source"] == "peeringdb"
    assert provenance["fetched_at"]
    assert provenance["record_updated"].startswith("2026-08-31")


@respx.mock
async def test_result_says_the_data_is_self_reported() -> None:
    """The caveat travels with the data, not in documentation nobody reads."""
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=fixture("net_as3320.json")))

    payload = await call("AS3320")

    assert "themselves" in (payload["note"] or "")


# --- Name search -----------------------------------------------------------


@respx.mock
async def test_ambiguous_name_returns_candidates_not_a_guess() -> None:
    respx.get(f"{API}/net").mock(
        return_value=httpx.Response(200, json=fixture("net_name_hurricane.json"))
    )

    payload = await call("Hurricane")

    assert payload["status"] == Status.AMBIGUOUS
    assert payload["data"]["network"] is None, "an ambiguous name must not resolve to a guess"
    asns = [c["asn"] for c in payload["data"]["candidates"]]
    assert 6939 in asns
    assert "call this tool again" in (payload["note"] or "")


@respx.mock
async def test_ambiguous_is_not_ok() -> None:
    """The invariant the whole envelope rests on: `ok` means there is an answer.

    A caller that branches on status must be able to trust it without also
    inspecting the payload. If `ambiguous` were reported as `ok`, a model
    would read the status, reach for `data.network`, and find nothing there.
    """
    respx.get(f"{API}/net").mock(
        return_value=httpx.Response(200, json=fixture("net_name_hurricane.json"))
    )

    payload = await call("Hurricane")

    assert payload["status"] != Status.OK


@respx.mock
async def test_ambiguous_still_says_where_the_candidates_came_from() -> None:
    """It is a real upstream answer, so it carries provenance like any other."""
    respx.get(f"{API}/net").mock(
        return_value=httpx.Response(200, json=fixture("net_name_hurricane.json"))
    )

    payload = await call("Hurricane")

    assert payload["provenance"] is not None
    assert payload["provenance"]["source"] == "peeringdb"


@respx.mock
async def test_name_matching_one_network_resolves_directly() -> None:
    single = {"meta": {}, "data": fixture("net_name_hurricane.json")["data"][:1]}
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=single))

    payload = await call("Hurricane Electric")

    assert payload["status"] == Status.OK, "one match is an answer, not an ambiguity"
    assert payload["data"]["network"]["asn"] == 6939
    assert payload["data"]["candidates"] == []


# --- Everything that can go wrong ------------------------------------------


@respx.mock
async def test_unknown_asn_is_not_found_not_an_exception() -> None:
    """The T4 acceptance criterion."""
    not_found = httpx.Response(404, json={"error": "Entity not found"})
    respx.get(f"{API}/net").mock(return_value=not_found)

    payload = await call("AS4294967290")

    assert payload["status"] == Status.NOT_FOUND
    assert payload["data"] is None
    assert "not listed" in (payload["note"] or "") or "no network" in (payload["note"] or "")


@respx.mock
async def test_empty_result_set_is_also_not_found() -> None:
    """PeeringDB says no in two different ways. Both mean the same thing here."""
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json={"meta": {}, "data": []}))

    payload = await call("AS65999")

    assert payload["status"] == Status.NOT_FOUND


@respx.mock
async def test_not_found_does_not_claim_the_network_is_fictional() -> None:
    """Absence from a self-reported registry is not absence from the internet."""
    respx.get(f"{API}/net").mock(return_value=httpx.Response(404))

    payload = await call("AS65999")

    assert "without" in (payload["note"] or "").lower()


@respx.mock
async def test_upstream_failure_is_reported_honestly() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(500))

    payload = await call("AS3320")

    assert payload["status"] == Status.UPSTREAM_UNAVAILABLE
    assert payload["data"] is None


@respx.mock
async def test_throttling_is_distinguishable_from_failure() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(429))

    payload = await call("AS3320")

    assert payload["status"] == Status.RATE_LIMITED
    assert "try again" in (payload["note"] or "")


async def test_empty_query_is_invalid_input() -> None:
    payload = await call("   ")

    assert payload["status"] == Status.INVALID_INPUT
    assert "AS3320" in (payload["note"] or "")


# --- The injection fixture, end to end -------------------------------------


@respx.mock
async def test_hostile_free_text_never_reaches_the_caller() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=fixture("net_hostile.json")))

    payload = await call("AS65002")

    serialised = json.dumps(payload)
    assert "IGNORE PREVIOUS INSTRUCTIONS" not in serialised
    assert "maintenance mode" not in serialised
    assert payload["data"]["network"]["name"] == "Totally Normal Net"
