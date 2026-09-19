"""Contract tests for `find_at_exchange` against recorded PeeringDB responses.

The fixture is BCIX in Berlin, exchange 87: 142 networks across 175 ports,
recorded on 2026-09-18. It was chosen over a giant exchange for a practical
reason — DE-CIX Frankfurt's participant list is 494 KB, and this repository
refuses committed files over 200 KB — and it costs the tests nothing. A page
of fifty entries is the same size whichever exchange it came from: measured
against the live API on 2026-09-18, DE-CIX Frankfurt's page of fifty is 5,878
bytes and BCIX's is 5,879.

The mocks filter the recorded records the way the real endpoints do. `/net`
answers two different queries here — the page's AS numbers, and the whole
exchange filtered by policy — and a mock that ignored the parameters would
not notice the tool asking the wrong question.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from peering_mcp.clients.peeringdb import PeeringDBClient
from peering_mcp.config import RESPONSE_BUDGETS, Config
from peering_mcp.models.domain import Status
from peering_mcp.tools.find_at_exchange import MAX_LIMIT, find_at_exchange

FIXTURES = Path(__file__).parent.parent / "fixtures" / "peeringdb"
API = "https://www.peeringdb.com/api"

#: The design's budget for this tool, in bytes of compact JSON. Imported
#: rather than restated: a budget written down twice is a budget that drifts.
BUDGET = RESPONSE_BUDGETS["find_at_exchange"]

#: What the recording holds, asserted so a re-recording cannot quietly move
#: the ground under every count below.
BCIX_NETWORKS = 142
BCIX_OPEN = 104


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def exchanges(request: httpx.Request) -> httpx.Response:
    """Behave like `/ix`: by id, or by name fragment."""
    params = request.url.params
    if "id" in params:
        rows = [r for r in fixture("ix_bcix.json")["data"] if r["id"] == int(params["id"])]
        if not rows:
            return httpx.Response(404, json={"data": [], "meta": {"error": "Entity not found"}})
        return httpx.Response(200, json={"meta": {}, "data": rows})
    fragment = params["name__contains"].casefold()
    pool = fixture("ix_bcix.json")["data"] + fixture("ix_name_linx.json")["data"]
    rows = [r for r in pool if fragment in r["name"].casefold()]
    return httpx.Response(200, json={"meta": {}, "data": rows})


def networks(request: httpx.Request) -> httpx.Response:
    """Behave like `/net`: filtered by AS number, or by exchange and policy."""
    params = request.url.params
    rows = fixture("net_ix87.json")["data"]
    if "asn__in" in params:
        wanted = {int(value) for value in params["asn__in"].split(",")}
        rows = [r for r in rows if r["asn"] in wanted]
    if "policy_general" in params:
        rows = [r for r in rows if r["policy_general"] == params["policy_general"]]
    return httpx.Response(200, json={"meta": {}, "data": rows})


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.delenv("PEERINGDB_API_KEY", raising=False)
    monkeypatch.setenv("PEERING_MCP_MAX_RETRIES", "1")
    return Config.from_env()


@pytest.fixture
def bcix() -> dict[str, respx.Route]:
    return {
        "ix": respx.get(f"{API}/ix").mock(side_effect=exchanges),
        "netixlan": respx.get(f"{API}/netixlan").mock(
            return_value=httpx.Response(200, json=fixture("netixlan_ix87.json"))
        ),
        "net": respx.get(f"{API}/net").mock(side_effect=networks),
    }


async def call(config: Config, **kwargs: Any) -> Any:
    async with PeeringDBClient(config) as client:
        return await find_at_exchange(client, **kwargs)


def compact_size(result: Any) -> int:
    return len(result.model_dump_json())


# --- The recording is what the tests below assume ------------------------------


def test_the_fixture_still_holds_what_these_tests_claim() -> None:
    rows = fixture("net_ix87.json")["data"]
    ports = fixture("netixlan_ix87.json")["data"]

    assert len(rows) == BCIX_NETWORKS
    assert len([r for r in rows if r["policy_general"] == "Open"]) == BCIX_OPEN
    assert {r["asn"] for r in ports} == {r["asn"] for r in rows}, "same networks, both ways round"


# --- Resolving which exchange is meant -----------------------------------------


@respx.mock
async def test_an_id_skips_the_name_search(config: Config, bcix: dict[str, respx.Route]) -> None:
    result = await call(config, exchange="87")

    assert result.status is Status.OK
    assert result.data.exchange.exchange_id == 87
    assert result.data.exchange.name == "BCIX"
    assert result.data.exchange.city == "Berlin"
    assert bcix["ix"].calls.last.request.url.params["id"] == "87"


@respx.mock
@pytest.mark.parametrize("query", ["ix87", "IX87", " 87 "])
async def test_an_id_is_recognised_however_it_is_written(
    config: Config, bcix: dict[str, respx.Route], query: str
) -> None:
    result = await call(config, exchange=query)

    assert result.status is Status.OK
    assert result.data.exchange.exchange_id == 87


@respx.mock
async def test_a_name_matching_one_exchange_resolves(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    result = await call(config, exchange="BCIX")

    assert result.status is Status.OK
    assert result.data.exchange.exchange_id == 87
    assert bcix["ix"].calls.last.request.url.params["name__contains"] == "BCIX"


@respx.mock
async def test_a_name_matching_several_is_ambiguous(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    """Ten exchanges are called LINX, on four continents. Guessing is the bug."""
    result = await call(config, exchange="LINX")

    assert result.status is Status.AMBIGUOUS
    assert result.data.exchange is None
    assert result.data.networks is None
    assert len(result.data.candidates) == 5, "capped: a choice, not a list to scroll"
    assert "10 exchanges match" in result.note
    assert "exchange_id" in result.note
    names = [candidate.name for candidate in result.data.candidates]
    assert "LINX LON1" in names
    assert all(candidate.exchange_id for candidate in result.data.candidates)


@respx.mock
async def test_an_unknown_id_is_not_found(config: Config, bcix: dict[str, respx.Route]) -> None:
    result = await call(config, exchange="999999")

    assert result.status is Status.NOT_FOUND
    assert "no exchange with id 999999" in result.note
    assert "search by name" in result.note, "an id is not corrected by a shorter fragment"
    assert not bcix["netixlan"].called, "nothing to ask about"


@respx.mock
async def test_a_name_matching_nothing_is_not_found(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    result = await call(config, exchange="no such exchange")

    assert result.status is Status.NOT_FOUND
    assert "no such exchange" in result.note
    assert "try a shorter fragment" in result.note


# --- The participant list ------------------------------------------------------


@respx.mock
async def test_the_page_is_a_slice_with_the_total(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    result = await call(config, exchange="87")

    page = result.data.networks
    assert page.total == BCIX_NETWORKS
    assert page.truncated is True
    assert 0 < len(page.items) <= 50, "the limit is a ceiling, the budget may lower it"
    assert f"Showing the {len(page.items)} largest of 142 networks" in result.note


@respx.mock
async def test_the_page_is_largest_ports_first(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    result = await call(config, exchange="87")

    speeds = [entry.speed_mbps or 0 for entry in result.data.networks.items]
    assert speeds == sorted(speeds, reverse=True)


@respx.mock
async def test_ports_are_summed_per_network(config: Config, bcix: dict[str, respx.Route]) -> None:
    """175 port rows, 142 networks: somebody at BCIX has more than one port.

    The total counts networks rather than rows, which is the fold. The page
    itself is shorter than the total here, because 142 entries do not fit the
    answer budget — a different claim, tested below.
    """
    result = await call(config, exchange="87", limit=MAX_LIMIT)

    assert result.data.networks.total == BCIX_NETWORKS, "networks, not port rows"
    assert max(entry.ports for entry in result.data.networks.items) > 1


@respx.mock
async def test_every_network_on_the_page_has_a_name_and_a_policy(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    """The `/net` call asks for exactly the page, so all of them resolve."""
    result = await call(config, exchange="87")

    for entry in result.data.networks.items:
        assert entry.name is not None, entry
        assert entry.policy is not None, entry
    requested = {
        int(value) for value in bcix["net"].calls.last.request.url.params["asn__in"].split(",")
    }
    assert len(requested) == 50, "names are asked for once, for the page the limit allows"
    assert {entry.asn for entry in result.data.networks.items} <= requested, (
        "the budget can trim the page after the names are fetched, never extend it"
    )


@respx.mock
async def test_a_short_list_is_not_truncated(config: Config, bcix: dict[str, respx.Route]) -> None:
    """A handful of networks fits any limit and any budget, so nothing is cut."""
    rows = fixture("netixlan_ix87.json")["data"][:3]
    bcix["netixlan"].mock(return_value=httpx.Response(200, json={"meta": {}, "data": rows}))

    result = await call(config, exchange="87", limit=MAX_LIMIT)

    assert result.data.networks.truncated is False
    assert "Showing" not in result.note


@respx.mock
async def test_an_exchange_nobody_records_a_port_at_is_not_recorded(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    """`/netixlan` answers 200 with an empty list, which is not the same as 404."""
    bcix["netixlan"].mock(return_value=httpx.Response(200, json={"meta": {}, "data": []}))

    result = await call(config, exchange="87")

    assert result.status is Status.NOT_RECORDED
    assert result.data is None
    assert "BCIX" in result.note
    assert "142 networks" in result.note, "its own record disagrees; say so"
    assert "not evidence of absence" in result.note


# --- The policy filter ---------------------------------------------------------


@respx.mock
async def test_the_policy_filter_is_applied_to_the_exchange_not_the_page(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    result = await call(config, exchange="87", policy="Open")

    page = result.data.networks
    assert page.total == BCIX_OPEN, "104 of 142, not 'the Open ones among the first 50'"
    assert all(entry.policy == "Open" for entry in page.items)
    params = bcix["net"].calls.last.request.url.params
    assert params["ix"] == "87"
    assert params["policy_general"] == "Open"
    assert "asn__in" not in params
    assert "(of 142 here) stating policy Open" in result.note


@respx.mock
@pytest.mark.parametrize("given", ["open", "OPEN", " Open "])
async def test_a_policy_is_canonicalised_before_it_is_sent(
    config: Config, bcix: dict[str, respx.Route], given: str
) -> None:
    """PeeringDB is asked in its own spelling, or the client reads a working
    filter as an ignored one."""
    result = await call(config, exchange="87", policy=given)

    assert result.status is Status.OK
    assert bcix["net"].calls.last.request.url.params["policy_general"] == "Open"


@respx.mock
async def test_a_filter_matching_nobody_is_an_answer_not_an_absence(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    """The note has to carry what the empty list cannot: they are there, and
    none of them say that."""
    result = await call(config, exchange="87", policy="No")
    assert result.status is Status.OK

    bcix["net"].mock(return_value=httpx.Response(200, json={"meta": {}, "data": []}))
    result = await call(config, exchange="87", policy="Restrictive")

    assert result.status is Status.OK
    assert result.data.networks.total == 0
    assert result.data.networks.items == []
    assert "None of the 142 networks at BCIX state policy Restrictive" in result.note


@respx.mock
async def test_an_unknown_policy_is_refused_before_anything_is_fetched(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    result = await call(config, exchange="87", policy="friendly")

    assert result.status is Status.INVALID_INPUT
    assert "Open, Selective, Restrictive, No" in result.note
    assert not bcix["ix"].called


@respx.mock
async def test_an_empty_ix_answer_is_the_other_way_upstream_says_no(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    """PeeringDB says "no such exchange" with a 404 and with an empty list.

    Both mean the same thing and only one of them raises, so the empty case
    has to be collapsed by hand or a typo reads as an exchange with no name.
    """
    bcix["ix"].mock(return_value=httpx.Response(200, json={"meta": {}, "data": []}))

    result = await call(config, exchange="87")

    assert result.status is Status.NOT_FOUND
    assert not bcix["netixlan"].called


@respx.mock
async def test_a_handful_of_candidates_is_not_reported_as_a_cut_list(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    result = await call(config, exchange="LINX LON")

    assert result.status is Status.AMBIGUOUS
    assert [c.name for c in result.data.candidates] == ["LINX LON1", "LINX LON2"]
    assert "Showing the first" not in result.note, "nothing was left out"


@respx.mock
async def test_a_filter_that_fits_on_one_page_says_how_many_of_how_many(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    """Four of 142 networks at BCIX are Restrictive. The count is the answer
    to a question the list alone does not answer: is that few, or is that all?"""
    result = await call(config, exchange="87", policy="Restrictive")

    assert result.status is Status.OK
    assert result.data.networks.total == 4
    assert result.data.networks.truncated is False
    assert "4 of the 142 networks here state policy Restrictive" in result.note


@respx.mock
async def test_an_empty_exchange_that_claims_nothing_does_not_claim_a_disagreement(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    """An exchange with no participants and no count is simply empty. The
    note only accuses PeeringDB of contradicting itself when it does."""
    record = fixture("ix_bcix.json")["data"][0] | {"net_count": 0}
    bcix["ix"].mock(return_value=httpx.Response(200, json={"meta": {}, "data": [record]}))
    bcix["netixlan"].mock(return_value=httpx.Response(200, json={"meta": {}, "data": []}))

    result = await call(config, exchange="87")

    assert result.status is Status.NOT_RECORDED
    assert "disagree" not in result.note
    assert "not evidence of absence" in result.note


# --- Budget --------------------------------------------------------------------


@respx.mock
async def test_the_default_page_fits_the_budget(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    """5,879 bytes of 6,144 when this was written: it fits, with 4% to spare.

    The margin is thin because a page of fifty is mostly network names, and a
    name may be 200 characters. Real exchanges land here; a pathological one
    would not. Recorded rather than fixed, because whether to cap the names
    is a decision for the budget task, not this one — and if a field is ever
    added to an entry, this test is what says no.
    """
    result = await call(config, exchange="87")

    assert compact_size(result) <= BUDGET


@respx.mock
async def test_the_cap_holds_the_largest_answer_down(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    result = await call(config, exchange="87", limit=10_000)

    page = result.data.networks
    assert len(page.items) < BCIX_NETWORKS, "clamped to MAX_LIMIT, then to what fits"
    assert page.total == BCIX_NETWORKS, "and it still says how many there are"
    assert page.truncated is True
    assert compact_size(result) <= BUDGET, "no limit buys a response over the line"
    assert "answer budget" in result.note, "and the note says asking again will not help"


# --- Input --------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize("given", ["", "   "])
async def test_an_empty_exchange_is_invalid(
    config: Config, bcix: dict[str, respx.Route], given: str
) -> None:
    result = await call(config, exchange=given)

    assert result.status is Status.INVALID_INPUT
    assert not bcix["ix"].called


@respx.mock
async def test_a_limit_below_one_is_invalid(config: Config, bcix: dict[str, respx.Route]) -> None:
    result = await call(config, exchange="87", limit=0)

    assert result.status is Status.INVALID_INPUT
    assert str(MAX_LIMIT) in result.note


# --- Upstream failure, through the shared mapping ------------------------------


@respx.mock
async def test_throttling_is_rate_limited(config: Config) -> None:
    respx.get(f"{API}/ix").mock(return_value=httpx.Response(429))

    result = await call(config, exchange="87")

    assert result.status is Status.RATE_LIMITED
    assert "throttling" in result.note


@respx.mock
async def test_an_unreachable_upstream_says_so(config: Config) -> None:
    respx.get(f"{API}/ix").mock(side_effect=httpx.ConnectError("no route to host"))

    result = await call(config, exchange="87")

    assert result.status is Status.UPSTREAM_UNAVAILABLE
    assert "Could not reach PeeringDB" in result.note


@respx.mock
async def test_a_broken_envelope_is_not_read_as_an_empty_exchange(
    config: Config, bcix: dict[str, respx.Route]
) -> None:
    """The T5 lesson, checked once more: malformed is not the same as empty."""
    bcix["netixlan"].mock(return_value=httpx.Response(200, json={"meta": {}}))

    result = await call(config, exchange="87")

    assert result.status is Status.UPSTREAM_UNAVAILABLE
