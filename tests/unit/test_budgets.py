"""Every response-size budget, enforced against the worst case the caps allow.

The contract tests already measure real recorded data against these lines.
This file asks the harder question: **not whether today's data fits, but
whether anything that can get through the shaping layer fits.** The two
answers were different until T12 — a page of fifty at the old 200-character
name cap measured 15,838 bytes against a 6 KB line, and the tests were green
the whole time, because no real exchange is full of 200-character names.

So every value here is built at its cap rather than sampled: names at
`LIST_ENTRY_NAME_LIMIT`, descriptors at `DETAIL_FIELD_LIMIT`, links at
`URL_LIMIT`, notes at the longest any tool writes. If a budget can be
exceeded, it is exceeded here.

**When one of these fails, the answer is almost never a bigger budget.** It is
a field that should not be in the payload, a cap that is too loose, or a list
that should be shorter. The budget is the requirement; the response is the
thing that has to move.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from peering_mcp.config import LIST_ENVELOPE_RESERVE, RESPONSE_BUDGETS, list_budget
from peering_mcp.models.domain import (
    CommonPresence,
    Exchange,
    ExchangeMatch,
    ExchangeParticipant,
    ExchangeParticipants,
    ExchangePresence,
    FacilityPresence,
    Network,
    NetworkLookup,
    NetworkMatch,
    Page,
    ParticipantPorts,
    PeeringPolicy,
    PresenceList,
    PresenceTotals,
    Provenance,
    Registration,
    RegistrationContact,
    SharedExchange,
    Status,
    ToolResult,
)
from peering_mcp.models.upstream import MAX_TEXT_LENGTH
from peering_mcp.sanitize import DEFAULT_MAX_LENGTH
from peering_mcp.shaping import (
    DETAIL_FIELD_LIMIT,
    IDENTIFIER_LIMIT,
    LIST_ENTRY_NAME_LIMIT,
    URL_LIMIT,
)

#: The longest note any tool writes, measured across the five of them and
#: rounded up. A note is prose, so it is the one field with no structural cap.
LONGEST_NOTE = 320

NAME = "N" * DEFAULT_MAX_LENGTH
ENTRY_NAME = "E" * LIST_ENTRY_NAME_LIMIT
DETAIL = "D" * DETAIL_FIELD_LIMIT
HANDLE = "H" * IDENTIFIER_LIMIT
URL = "U" * URL_LIMIT
NOTE = "A" * LONGEST_NOTE

#: The largest numbers upstream can express, since digits cost bytes too.
BIG_ASN = 4_294_967_294
BIG_COUNT = 9_999_999


def size(result: ToolResult[Any]) -> int:
    """What the caller actually receives, in bytes of compact JSON."""
    return len(result.model_dump_json())


def provenance() -> Provenance:
    return Provenance.now("peeringdb", record_updated=datetime.now(UTC), from_cache=False)


# --- The two tools that answer with one object ------------------------------


def worst_network() -> ToolResult[NetworkLookup]:
    return ToolResult(
        status=Status.OK,
        data=NetworkLookup(
            network=Network(
                asn=BIG_ASN,
                name=NAME,
                long_name=NAME,
                website=URL,
                network_type=DETAIL,
                traffic_estimate=DETAIL,
                scope=DETAIL,
                traffic_ratio=DETAIL,
                ipv4_prefixes=BIG_COUNT,
                ipv6_prefixes=BIG_COUNT,
                exchange_count=BIG_COUNT,
                facility_count=BIG_COUNT,
                policy=PeeringPolicy(
                    general=DETAIL,
                    locations=DETAIL,
                    ratio_required=True,
                    contract_required=DETAIL,
                    url=URL,
                ),
                irr_as_set=DETAIL,
                looking_glass=URL,
            )
        ),
        note=NOTE,
        provenance=provenance(),
    )


def worst_candidates() -> ToolResult[NetworkLookup]:
    """The other shape `lookup_network` can return: five full-length names."""
    return ToolResult(
        status=Status.AMBIGUOUS,
        data=NetworkLookup(candidates=[NetworkMatch(asn=BIG_ASN, name=NAME) for _ in range(5)]),
        note=NOTE,
        provenance=provenance(),
    )


def worst_registration() -> ToolResult[Registration]:
    return ToolResult(
        status=Status.OK,
        data=Registration(
            target="2001:0db8:ffff:ffff:ffff:ffff:ffff:ffff/128",
            kind="prefix",
            registry=DETAIL,
            handle=HANDLE,
            holder=NAME,
            covers=HANDLE,
            country=DETAIL,
            allocation_type=DETAIL,
            status=[DETAIL] * 4,
            registered=datetime.now(UTC),
            abuse=RegistrationContact(name=NAME, email=DETAIL),
        ),
        note=NOTE,
        provenance=Provenance.now("rdap", record_updated=datetime.now(UTC)),
    )


def worst_exchange_candidates() -> ToolResult[ExchangeParticipants]:
    """The ambiguous shape: five exchanges to choose between, no list at all.

    It shares `find_at_exchange`'s budget without sharing its structure, which
    is why it is measured here beside the object answers rather than with the
    pages.
    """
    return ToolResult(
        status=Status.AMBIGUOUS,
        data=ExchangeParticipants(
            candidates=[
                ExchangeMatch(exchange_id=BIG_COUNT, name=NAME, city=NAME, country="DE")
                for _ in range(5)
            ]
        ),
        note=NOTE,
        provenance=provenance(),
    )


@pytest.mark.parametrize(
    ("tool", "build"),
    [
        ("lookup_network", worst_network),
        ("lookup_network", worst_candidates),
        ("lookup_registration", worst_registration),
        ("find_at_exchange", worst_exchange_candidates),
    ],
)
def test_an_object_answer_fits_its_budget(tool: str, build: Any) -> None:
    result = build()

    assert size(result) <= RESPONSE_BUDGETS[tool], (
        f"{tool} can exceed its budget: {size(result)} > {RESPONSE_BUDGETS[tool]}"
    )


# --- The three tools that answer with a list --------------------------------


def worst_presence(count: int) -> ToolResult[PresenceList]:
    exchanges = Page[ExchangePresence].within_budget(
        [
            ExchangePresence(
                name=ENTRY_NAME,
                city=ENTRY_NAME,
                country="DE",
                speed_mbps=BIG_COUNT,
                ports=999,
                route_server=True,
            )
            for _ in range(count)
        ],
        total=BIG_COUNT,
        budget=list_budget("list_presence"),
    )
    facilities = Page[FacilityPresence].within_budget(
        [FacilityPresence(name=ENTRY_NAME, city=ENTRY_NAME, country="DE") for _ in range(count)],
        total=BIG_COUNT,
        budget=list_budget("list_presence"),
    )
    return ToolResult(
        status=Status.OK,
        data=PresenceList(asn=BIG_ASN, network=NAME, exchanges=exchanges, facilities=facilities),
        note=NOTE,
        provenance=provenance(),
    )


def worst_participants(count: int) -> ToolResult[ExchangeParticipants]:
    networks = Page[ExchangeParticipant].within_budget(
        [
            ExchangeParticipant(
                asn=BIG_ASN,
                name=ENTRY_NAME,
                speed_mbps=BIG_COUNT,
                ports=999,
                route_server=True,
                policy=DETAIL,
            )
            for _ in range(count)
        ],
        total=BIG_COUNT,
        budget=list_budget("find_at_exchange"),
    )
    return ToolResult(
        status=Status.OK,
        data=ExchangeParticipants(
            exchange=Exchange(
                exchange_id=BIG_COUNT,
                name=NAME,
                city=NAME,
                country="DE",
                networks_recorded=BIG_COUNT,
            ),
            networks=networks,
        ),
        note=NOTE,
        provenance=provenance(),
    )


def worst_common(count: int) -> ToolResult[CommonPresence]:
    """Five networks, the most `find_common_presence` accepts, at every cap."""
    exchanges = Page[SharedExchange].within_budget(
        [
            SharedExchange(
                name=ENTRY_NAME,
                city=ENTRY_NAME,
                country="DE",
                networks=[
                    ParticipantPorts(
                        asn=BIG_ASN, speed_mbps=BIG_COUNT, ports=999, route_server=True
                    )
                    for _ in range(5)
                ],
            )
            for _ in range(count)
        ],
        total=BIG_COUNT,
        budget=list_budget("find_common_presence"),
    )
    facilities = Page[FacilityPresence].within_budget(
        [FacilityPresence(name=ENTRY_NAME, city=ENTRY_NAME, country="DE") for _ in range(count)],
        total=BIG_COUNT,
        budget=list_budget("find_common_presence"),
    )
    return ToolResult(
        status=Status.OK,
        data=CommonPresence(
            networks=[
                PresenceTotals(asn=BIG_ASN, name=NAME, exchanges=BIG_COUNT, facilities=BIG_COUNT)
                for _ in range(5)
            ],
            exchanges=exchanges,
            facilities=facilities,
        ),
        note=NOTE,
        provenance=provenance(),
    )


@pytest.mark.parametrize("count", [1, 25, 50, 200])
@pytest.mark.parametrize(
    ("tool", "build"),
    [
        ("list_presence", worst_presence),
        ("find_at_exchange", worst_participants),
        ("find_common_presence", worst_common),
    ],
)
def test_a_list_answer_fits_its_budget_at_every_page_size(
    tool: str, build: Any, count: int
) -> None:
    """Including 200, the hard cap, which no default ever reaches."""
    result = build(count)
    budget = RESPONSE_BUDGETS[tool]
    lists = _pages(result)

    for page in lists:
        spent = sum(len(item.model_dump_json()) + 1 for item in page.items)
        assert spent <= list_budget(tool), f"{tool}: a list spent {spent} of {list_budget(tool)}"

    # Per list, not per response: `list_presence` with kind="both" carries two
    # of them, and each is held to the line on its own.
    assert size(result) <= budget * len(lists), (
        f"{tool}: {size(result)} over {budget} x {len(lists)}"
    )


def _pages(result: ToolResult[Any]) -> list[Page[Any]]:
    data = result.data
    assert data is not None
    if isinstance(data, PresenceList):
        return [page for page in (data.exchanges, data.facilities) if page is not None]
    if isinstance(data, ExchangeParticipants):
        assert data.networks is not None
        return [data.networks]
    assert isinstance(data, CommonPresence)
    return [data.exchanges, data.facilities]


# --- What the budget is made of ---------------------------------------------


def test_the_envelope_reserve_covers_the_largest_envelope() -> None:
    """A list's budget is the tool's budget minus what the envelope around it
    costs. If the envelope grows past the reserve — a longer note, another
    field on the object a list belongs to — the list budget is wrong and every
    other test here is measuring the wrong thing."""
    empty = ToolResult[ExchangeParticipants](
        status=Status.OK,
        data=ExchangeParticipants(
            exchange=Exchange(
                exchange_id=BIG_COUNT,
                name=NAME,
                city=NAME,
                country="DE",
                networks_recorded=BIG_COUNT,
            ),
            networks=Page[ExchangeParticipant](items=[], total=BIG_COUNT, truncated=True),
        ),
        note=NOTE,
        provenance=provenance(),
    )

    assert size(empty) <= LIST_ENVELOPE_RESERVE, (
        f"the envelope costs {size(empty)}, over the {LIST_ENVELOPE_RESERVE} reserved for it"
    )


def test_every_registered_tool_has_a_budget() -> None:
    """A tool nobody budgeted is a tool nobody measured."""
    import asyncio

    from peering_mcp.server import mcp

    registered = {tool.name for tool in asyncio.run(mcp.list_tools())}

    assert registered == set(RESPONSE_BUDGETS), "every tool, and nothing that is not a tool"


def test_the_caps_are_tighter_than_the_sanitiser_s() -> None:
    """These limits only make sense below the one the boundary already applies;
    above it they would be decoration."""
    assert LIST_ENTRY_NAME_LIMIT < MAX_TEXT_LENGTH
    assert DETAIL_FIELD_LIMIT < LIST_ENTRY_NAME_LIMIT
    assert IDENTIFIER_LIMIT < MAX_TEXT_LENGTH
    assert URL_LIMIT < MAX_TEXT_LENGTH


# --- The page that fits by construction -------------------------------------


def test_a_page_keeps_what_fits_and_says_what_it_cut() -> None:
    items = [FacilityPresence(name=ENTRY_NAME, city=ENTRY_NAME, country="DE") for _ in range(100)]

    page = Page[FacilityPresence].within_budget(items, total=500, budget=1000)

    assert 0 < len(page.items) < 100
    assert page.total == 500
    assert page.truncated is True
    assert sum(len(item.model_dump_json()) + 1 for item in page.items) <= 1000


def test_a_page_that_fits_whole_is_not_truncated() -> None:
    items = [FacilityPresence(name="X", city="Y", country="DE") for _ in range(3)]

    page = Page[FacilityPresence].within_budget(items, total=3, budget=10_000)

    assert len(page.items) == 3
    assert page.truncated is False


def test_a_budget_too_small_for_one_entry_returns_none_rather_than_one_too_many() -> None:
    """The guarantee is the point: an empty page with an honest total beats a
    page that breaks the line it exists to keep."""
    items = [FacilityPresence(name=ENTRY_NAME, city=ENTRY_NAME, country="DE")]

    page = Page[FacilityPresence].within_budget(items, total=1, budget=10)

    assert page.items == []
    assert page.truncated is True
