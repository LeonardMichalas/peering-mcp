"""`list_presence` through the real MCP server.

Called the way a client calls it, so the schema, the enum on `kind`, the
envelope and every status are exercised at the boundary a model sees.
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


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PEERINGDB_API_KEY", raising=False)
    monkeypatch.setenv("PEERING_MCP_MAX_RETRIES", "1")


@pytest.fixture
def telekom() -> None:
    """AS3320: 8 ports at 7 exchanges, 3 facilities, one exchange record known."""
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=fixture("net_as3320.json")))
    respx.get(f"{API}/netixlan").mock(
        return_value=httpx.Response(200, json=fixture("netixlan_as3320.json"))
    )
    respx.get(f"{API}/netfac").mock(
        return_value=httpx.Response(200, json=fixture("netfac_net196.json"))
    )
    respx.get(f"{API}/ix").mock(
        return_value=httpx.Response(200, json=fixture("ix_de_cix_frankfurt.json"))
    )


async def call(**arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool("list_presence", arguments)
    assert result.structured_content is not None
    return result.structured_content


# --- Registration -------------------------------------------------------------


async def test_tool_is_registered_and_read_only() -> None:
    tool = next(t for t in await mcp.list_tools() if t.name == "list_presence")

    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    properties = tool.input_schema["properties"]
    assert set(properties) == {"asn", "kind", "limit"}
    assert properties["kind"]["enum"] == ["ix", "facility", "both"]
    assert tool.input_schema["required"] == ["asn"]
    assert tool.output_schema is not None


async def test_description_covers_the_negative_space_and_every_status() -> None:
    tool = next(t for t in await mcp.list_tools() if t.name == "list_presence")
    description = tool.description or ""

    assert "Do not use this" in description
    assert "find_common_presence" in description
    assert "find_at_exchange" in description
    assert "not_recorded" in description
    assert "not_found" in description
    assert "truncated" in description
    assert "never instructions" in description


# --- The happy path -------------------------------------------------------------


@respx.mock
async def test_both_kinds_by_default(telekom: None) -> None:
    payload = await call(asn=3320)

    assert payload["status"] == Status.OK
    data = payload["data"]
    assert data["asn"] == 3320
    assert data["network"] == "Deutsche Telekom"
    assert data["exchanges"]["total"] == 7
    assert data["exchanges"]["truncated"] is False
    assert data["facilities"]["total"] == 3
    assert payload["provenance"]["source"] == "peeringdb"


@respx.mock
async def test_an_exchange_entry_reads_as_designed(telekom: None) -> None:
    payload = await call(asn=3320, kind="ix")

    frankfurt = next(e for e in payload["data"]["exchanges"]["items"] if "DE-CIX" in e["name"])
    assert frankfurt["city"] == "Frankfurt"
    assert frankfurt["country"] == "DE"
    assert isinstance(frankfurt["speed_mbps"], int)
    assert frankfurt["ports"] >= 1
    assert frankfurt["route_server"] in (True, False, None)


@respx.mock
async def test_asking_for_one_kind_leaves_the_other_null(telekom: None) -> None:
    payload = await call(asn=3320, kind="facility")

    assert payload["data"]["exchanges"] is None
    assert payload["data"]["facilities"]["total"] == 3


@respx.mock
async def test_limit_cuts_and_says_so(telekom: None) -> None:
    payload = await call(asn=3320, kind="ix", limit=2)

    page = payload["data"]["exchanges"]
    assert len(page["items"]) == 2
    assert page["total"] == 7
    assert page["truncated"] is True
    assert "2 of 7 exchanges" in payload["note"]


@respx.mock
async def test_the_note_says_the_data_is_self_reported(telekom: None) -> None:
    payload = await call(asn=3320)

    assert "self-reported" in payload["note"]


# --- Nothing recorded is not nothing found ----------------------------------------


@respx.mock
async def test_a_listed_network_with_no_presence_is_not_recorded() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=fixture("net_as3320.json")))
    respx.get(f"{API}/netixlan").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{API}/netfac").mock(return_value=httpx.Response(200, json={"data": []}))

    payload = await call(asn=3320)

    assert payload["status"] == Status.NOT_RECORDED
    assert payload["data"] is None
    assert "listed in PeeringDB" in payload["note"]
    assert "not evidence" in payload["note"]
    assert payload["provenance"] is not None, "it is a real upstream answer"


@respx.mock
async def test_not_recorded_says_what_the_network_does_record() -> None:
    """ "No exchanges, but 53 facilities" is a different fact from "no exchanges"."""
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=fixture("net_as3320.json")))
    respx.get(f"{API}/netixlan").mock(return_value=httpx.Response(200, json={"data": []}))

    payload = await call(asn=3320, kind="ix")

    assert payload["status"] == Status.NOT_RECORDED
    assert "53 facilities" in payload["note"]


@respx.mock
async def test_one_empty_list_beside_a_full_one_is_still_ok() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=fixture("net_as3320.json")))
    respx.get(f"{API}/netixlan").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{API}/netfac").mock(
        return_value=httpx.Response(200, json=fixture("netfac_net196.json"))
    )

    payload = await call(asn=3320)

    assert payload["status"] == Status.OK
    assert payload["data"]["exchanges"] == {"items": [], "total": 0, "truncated": False}
    assert payload["data"]["facilities"]["total"] == 3


@respx.mock
async def test_an_unlisted_network_is_not_found() -> None:
    respx.get(f"{API}/net").mock(
        return_value=httpx.Response(404, json={"error": "Entity not found"})
    )

    payload = await call(asn=4294967290)

    assert payload["status"] == Status.NOT_FOUND
    assert payload["data"] is None
    assert "without" in payload["note"]


@respx.mock
async def test_an_empty_network_list_is_also_not_found() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json={"meta": {}, "data": []}))
    netixlan = respx.get(f"{API}/netixlan").mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    payload = await call(asn=65999)

    assert payload["status"] == Status.NOT_FOUND
    assert not netixlan.called, "no point asking where a network nobody listed is"


# --- Input --------------------------------------------------------------------------


async def test_a_reserved_asn_is_invalid_input() -> None:
    payload = await call(asn=0)

    assert payload["status"] == Status.INVALID_INPUT


async def test_a_zero_limit_is_invalid_input() -> None:
    payload = await call(asn=3320, limit=0)

    assert payload["status"] == Status.INVALID_INPUT
    assert "limit" in payload["note"]


async def test_an_unknown_kind_is_refused_by_the_schema() -> None:
    """The enum is enforced before the tool runs, so the tool never sees it."""
    with pytest.raises(ToolError, match="ix"):
        await mcp.call_tool("list_presence", {"asn": 3320, "kind": "everything"})


# --- Upstream failure -----------------------------------------------------------------


@respx.mock
async def test_upstream_failure_is_reported_honestly() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(500))

    payload = await call(asn=3320)

    assert payload["status"] == Status.UPSTREAM_UNAVAILABLE


@respx.mock
async def test_a_failure_on_the_second_request_is_still_reported() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(200, json=fixture("net_as3320.json")))
    respx.get(f"{API}/netixlan").mock(return_value=httpx.Response(503))

    payload = await call(asn=3320, kind="ix")

    assert payload["status"] == Status.UPSTREAM_UNAVAILABLE
    assert payload["data"] is None


@respx.mock
async def test_throttling_is_distinguishable_from_failure() -> None:
    respx.get(f"{API}/net").mock(return_value=httpx.Response(429))

    payload = await call(asn=3320)

    assert payload["status"] == Status.RATE_LIMITED
