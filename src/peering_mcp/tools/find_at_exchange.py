"""The `find_at_exchange` tool.

Who is at an exchange: the inverse of `list_presence`, and the question a
peering coordinator asks second. The first is where can we meet this network;
this one is who else is in the building we are already in.

Three upstream requests at most:

1. `/ix` — which exchange is meant. By id when the caller gives one, by name
   otherwise, and a name matching several exchanges comes back as `ambiguous`
   rather than resolved to the largest. This step is also what tells a real
   exchange from an invented one: `/netixlan` answers 200 with an empty list
   for any `ix_id`, so without it a typo would read as an empty exchange.
2. `/netixlan?ix_id=` — every port every network has there, one row each.
3. `/net` — the names and policies, which the port rows do not carry. Which
   networks are asked about depends on the filter, and that is the only place
   the two paths differ:

   * with no policy filter, only the networks on the page being returned —
     fifty records rather than the exchange's thousand;
   * with one, every network at the exchange whose policy matches, because a
     filter applied after the page was cut would filter the page instead of
     the exchange. That answer carries the names and policies too, so it
     replaces the page lookup rather than adding to it.
"""

from __future__ import annotations

import re
from datetime import datetime

from peering_mcp.clients.http import Fetched
from peering_mcp.clients.peeringdb import NAME_MATCH_LIMIT, PeeringDBClient
from peering_mcp.config import list_budget
from peering_mcp.errors import UpstreamError, UpstreamNotFoundError
from peering_mcp.models.domain import (
    ExchangeParticipant,
    ExchangeParticipants,
    Page,
    Provenance,
    Status,
    ToolResult,
)
from peering_mcp.models.upstream import UpstreamExchange, UpstreamNetwork, UpstreamNetworkIxLan
from peering_mcp.shaping import (
    group_by_network,
    shape_exchange,
    shape_exchange_match,
    shape_participant,
)
from peering_mcp.tools._envelope import upstream_failure

DEFAULT_LIMIT = 50
#: The same cap as `list_presence`, because the entries are the same size and
#: a caller who wants more than this is better served by a narrower question.
MAX_LIMIT = 200

#: The four values PeeringDB accepts for `policy_general`, spelled its way.
#: Sent verbatim as an upstream filter, so the spelling is load-bearing: the
#: client checks the returned records against what it asked for, and `open`
#: would read as a filter upstream had ignored.
POLICIES = ("Open", "Selective", "Restrictive", "No")

_BY_LOWER = {policy.lower(): policy for policy in POLICIES}

#: `31`, `ix31` or `IX31`. Anything else is treated as part of a name.
_ID_PATTERN = re.compile(r"^\s*(?:ix)?(\d+)\s*$", re.IGNORECASE)

_SELF_REPORTED = (
    "Participation is self-reported by each network in PeeringDB; "
    "a network missing here is unrecorded, not absent."
)


def parse_exchange_id(query: str) -> int | None:
    """Return the PeeringDB exchange id in `query`, or None if it is not one."""
    match = _ID_PATTERN.match(query)
    return int(match.group(1)) if match else None


def canonical_policy(policy: str) -> str | None:
    """PeeringDB's spelling of a policy name, or None if it is not one of them."""
    return _BY_LOWER.get(policy.strip().lower())


async def find_at_exchange(
    client: PeeringDBClient,
    exchange: str,
    policy: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> ToolResult[ExchangeParticipants]:
    """List the networks present at one internet exchange."""
    cleaned = exchange.strip()
    if not cleaned:
        return ToolResult(
            status=Status.INVALID_INPUT,
            note="Give an exchange name such as 'DE-CIX Frankfurt', or a PeeringDB id.",
        )

    wanted = None
    if policy is not None:
        wanted = canonical_policy(policy)
        if wanted is None:
            return ToolResult(
                status=Status.INVALID_INPUT,
                note=f"{policy!r} is not a peering policy; "
                f"use one of {', '.join(POLICIES)}, or leave it out for all networks.",
            )

    if limit < 1:
        return ToolResult(
            status=Status.INVALID_INPUT,
            note=f"limit must be at least 1; default {DEFAULT_LIMIT}, at most {MAX_LIMIT}.",
        )
    limit = min(limit, MAX_LIMIT)

    try:
        return await _find(client, cleaned, wanted, limit)
    except UpstreamNotFoundError:
        return _not_found(cleaned)
    except UpstreamError as exc:
        return upstream_failure(exc)


async def _find(
    client: PeeringDBClient, query: str, policy: str | None, limit: int
) -> ToolResult[ExchangeParticipants]:
    resolved = await _resolve(client, query)
    if isinstance(resolved, ToolResult):
        return resolved
    exchange, exchange_fetch = resolved

    ports = await client.participants(exchange.record_id)
    fetches: list[Fetched[object]] = [exchange_fetch, ports]
    present = group_by_network(ports.value)

    if not present:
        return ToolResult(
            status=Status.NOT_RECORDED,
            note=_nothing_recorded(exchange),
            provenance=_provenance(fetches, ports.value),
        )

    if policy is None:
        page = present[:limit]
        detail = await client.networks_by_asns(group.asn for group in page)
        by_asn = _by_asn(detail.value)
        total = len(present)
    else:
        detail = await client.networks_at_exchange(exchange.record_id, policy)
        by_asn = _by_asn(detail.value)
        matching = [group for group in present if group.asn in by_asn]
        page, total = matching[:limit], len(matching)
    fetches.append(detail)

    networks = Page[ExchangeParticipant].within_budget(
        [shape_participant(group, by_asn.get(group.asn)) for group in page],
        total=total,
        budget=list_budget("find_at_exchange"),
    )
    return ToolResult(
        status=Status.OK,
        data=ExchangeParticipants(exchange=shape_exchange(exchange), networks=networks),
        note=_note(exchange, networks, policy, len(present), limit),
        provenance=_provenance(fetches, ports.value),
    )


async def _resolve(
    client: PeeringDBClient, query: str
) -> tuple[UpstreamExchange, Fetched[object]] | ToolResult[ExchangeParticipants]:
    """Which exchange the caller means, or the envelope saying why that is not one.

    An id resolves or it does not. A name can match nothing, one exchange, or
    several — and several is `ambiguous` rather than a guess, because
    "Equinix" names dozens of exchanges on three continents and picking the
    first would send somebody to the wrong continent, confidently.
    """
    ix_id = parse_exchange_id(query)
    if ix_id is not None:
        fetched = await client.exchange_by_id(ix_id)
        if fetched.value is None:
            return _not_found(query)
        return fetched.value, fetched

    matches = await client.exchanges_by_name(query)
    records = matches.value
    if not records:
        return _not_found(query)
    if len(records) == 1:
        return records[0], matches

    note = (
        f"{len(records)} exchanges match {query!r}. "
        "Pick one and call this tool again with its exchange_id."
    )
    if len(records) > NAME_MATCH_LIMIT:
        note += f" Showing the first {NAME_MATCH_LIMIT}."
    return ToolResult(
        status=Status.AMBIGUOUS,
        data=ExchangeParticipants(
            candidates=[shape_exchange_match(row) for row in records[:NAME_MATCH_LIMIT]]
        ),
        note=note,
        provenance=Provenance.now("peeringdb", from_cache=matches.from_cache),
    )


def _by_asn(records: list[UpstreamNetwork]) -> dict[int, UpstreamNetwork]:
    return {record.asn: record for record in records}


def _note(
    exchange: UpstreamExchange,
    networks: Page[ExchangeParticipant],
    policy: str | None,
    present: int,
    limit: int,
) -> str:
    """The caveat, and one sentence for what the filter and the page left out.

    Two sentences at the outside, on the rule Checkpoint A carried forward: a
    note that runs on stops being read, and this one has to compete with the
    fifty networks underneath it.

    A filter that matches nothing is the case worth writing for. "No networks"
    and "none of the 142 networks here state that policy" are the same empty
    list and different answers, and only the second can be repeated to
    somebody without misleading them.
    """
    if policy is not None and not networks.total:
        return (
            f"None of the {present} networks at {exchange.name} state policy {policy}. "
            f"{_SELF_REPORTED}"
        )

    if not networks.truncated and policy is None:
        return _SELF_REPORTED

    if policy is None:
        shown = f"Showing the {len(networks.items)} largest of {networks.total} networks"
    elif networks.truncated:
        shown = (
            f"Showing the {len(networks.items)} largest of the {networks.total} networks "
            f"(of {present} here) stating policy {policy}"
        )
    else:
        return (
            f"{_SELF_REPORTED} {networks.total} of the {present} networks here "
            f"state policy {policy}."
        )
    if len(networks.items) < limit:
        # The budget cut the page before the limit did, so asking again with a
        # higher limit would return this same page.
        return f"{_SELF_REPORTED} {shown} — as much as the answer budget fits."
    return f"{_SELF_REPORTED} {shown}; raise limit for more, at most {MAX_LIMIT}."


def _nothing_recorded(exchange: UpstreamExchange) -> str:
    """Say the exchange is real and empty, and how empty PeeringDB thinks it is."""
    note = f"{exchange.name} is listed in PeeringDB, but no network records a port there."
    if exchange.net_count:
        note += (
            f" Its own record claims {exchange.net_count} networks, so the two disagree "
            "and the participant list is the one that is missing."
        )
    return note + " Absence here is not evidence of absence in reality."


def _not_found(query: str) -> ToolResult[ExchangeParticipants]:
    """Say what was not found, and what to try instead of it.

    An id and a name fail differently, and so does the way out: a bad id is
    corrected by searching, and a bad name by searching for less. Telling
    somebody who typed 999999 to try a shorter fragment is advice about a
    question they did not ask.
    """
    if parse_exchange_id(query) is not None:
        note = (
            f"PeeringDB has no exchange with id {query}. Ids come from a "
            "candidate list or an earlier answer; search by name instead."
        )
    else:
        note = (
            f"PeeringDB has no exchange matching {query!r}. Exchanges are listed "
            "under their own name, such as 'DE-CIX Frankfurt' or 'LINX LON1'; "
            "try a shorter fragment."
        )
    return ToolResult(status=Status.NOT_FOUND, note=note)


def _provenance(fetches: list[Fetched[object]], rows: list[UpstreamNetworkIxLan]) -> Provenance:
    return Provenance.now(
        "peeringdb",
        record_updated=_latest_edit(rows),
        from_cache=all(fetch.from_cache for fetch in fetches),
    )


def _latest_edit(rows: list[UpstreamNetworkIxLan]) -> datetime | None:
    """When any port row was last touched: the age of the participant list."""
    stamps = [row.updated for row in rows if row.updated is not None]
    return max(stamps) if stamps else None


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "POLICIES", "find_at_exchange"]
