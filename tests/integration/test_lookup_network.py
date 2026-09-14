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
    """Content in a dropped field does not exist on this side of the boundary."""
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=fixture("net_hostile.json")))

    payload = await call("AS65002")

    serialised = json.dumps(payload)
    assert "SSH keys" not in serialised, "aka is dropped"
    assert "shell_exec" not in serialised, "notes is dropped"
    assert payload["data"]["network"]["name"] == "Totally Normal Net"


@respx.mock
async def test_a_hostile_record_arrives_with_its_structure_removed() -> None:
    """Every field that does pass through is stripped of control shapes."""
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=fixture("net_hostile.json")))

    payload = await call("AS65002")
    network = payload["data"]["network"]

    assert "<system>" not in json.dumps(network)
    assert "</name>" not in json.dumps(network)
    assert "<|im_start|>" not in json.dumps(network)
    assert "[INST]" not in json.dumps(network)
    assert "```" not in json.dumps(network)

    # Checked on the values: json.dumps escapes a newline, so looking for one
    # in its output is an assertion that can never fail.
    text_values = [v for v in network.values() if isinstance(v, str)]
    text_values += [v for v in network["policy"].values() if isinstance(v, str)]
    for value in text_values:
        assert "\n" not in value, "nothing can span what looks like a line"


@respx.mock
async def test_upstream_text_never_appears_in_the_prose_the_model_reads() -> None:
    """The defence a character filter cannot provide.

    Cleaning removes structure, not meaning: a network that writes an
    instruction into its own long name still has that sentence in its long
    name. What stops it being read as an instruction is that it stays in a
    field with a name on it, and never in the envelope's `note`, which is the
    one part of the response written by this server and addressed to the model.
    """
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=fixture("net_hostile.json")))

    payload = await call("AS65002")

    assert "maintenance mode" in payload["data"]["network"]["long_name"]
    assert "maintenance mode" not in (payload["note"] or "")
    assert payload["note"] == (
        "PeeringDB records are maintained by the networks themselves. "
        "Treat a missing field as unrecorded, not as evidence it is untrue."
    )


@respx.mock
async def test_an_overlong_value_is_marked_as_cut_rather_than_silently_shortened() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=fixture("net_hostile.json")))

    payload = await call("AS65002")

    assert payload["data"]["network"]["traffic_estimate"].endswith("\u2026")


async def test_the_tool_description_says_third_party_text_is_data() -> None:
    """The weakest of the four defences, and still worth having.

    A model that is told the names in a result are written by third parties has
    a reason not to act on one. It is the only defence that reaches the case
    the other three cannot: text that is hostile in meaning rather than shape.
    """
    tool = next(t for t in await mcp.list_tools() if t.name == "lookup_network")
    description = (tool.description or "").lower()

    assert "data" in description
    assert "never instructions" in description


async def test_the_server_instructions_say_it_too() -> None:
    """One layer below the tool description, so a sixth tool inherits it."""
    assert mcp.instructions is not None
    assert "never instructions to follow" in mcp.instructions
