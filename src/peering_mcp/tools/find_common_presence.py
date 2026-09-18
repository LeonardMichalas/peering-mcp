"""The `find_common_presence` tool.

Where two to five networks can meet: the exchanges and facilities all of them
record a presence at.

**This is the question the project was built for.** Answering it by hand means
looking each network up, listing everywhere it is present, and intersecting the
results across browser tabs. The intersection itself is cheap; what is not
cheap is the part this module spends most of its lines on — saying why a result
is empty.

An empty answer has three different meanings, and a caller that cannot tell
them apart will guess:

* one of the networks is not listed in PeeringDB at all — `not_found`;
* one of them is listed but has entered no presence anywhere — `not_recorded`,
  and the per-network totals name which one;
* they are all well recorded and genuinely share nothing — `ok`, with empty
  lists and the totals that make that credible.

Four upstream requests at most, and the fourth only when there is something to
locate:

1. `/net?asn__in=` — whether each network exists, its name, and the PeeringDB
   id the facility query is keyed on.
2. `/netixlan?asn__in=` — every exchange port for every network, one request
   rather than one per network. Confirmed against the live API first.
3. `/netfac?net_id__in=` — every facility row, keyed by id because `/netfac`
   ignores an ASN filter and answers with the whole table.
4. `/ix?id__in=` — the exchanges on the page being returned, because port rows
   do not say where an exchange is. Skipped entirely when nothing is shared.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from peering_mcp.clients.http import Fetched
from peering_mcp.clients.peeringdb import PeeringDBClient
from peering_mcp.errors import UpstreamError, UpstreamNotFoundError
from peering_mcp.models.domain import (
    CommonPresence,
    FacilityPresence,
    Page,
    PresenceTotals,
    Provenance,
    SharedExchange,
    Status,
    ToolResult,
)
from peering_mcp.models.upstream import UpstreamRecord
from peering_mcp.shaping import (
    group_exchanges_by_asn,
    group_facilities_by_net_id,
    shape_facility_presence,
    shape_shared_exchange,
    shared_exchanges,
    shared_facilities,
)
from peering_mcp.tools._envelope import upstream_failure
from peering_mcp.tools.lookup_network import _MAX_ASN, _MIN_ASN

#: Two networks is the smallest question worth asking. Five is where the cold
#: path stops being tolerable, and where a model stops reading the answer.
MIN_ASNS = 2
MAX_ASNS = 5

DEFAULT_LIMIT = 25
#: The most shared locations one answer may carry. Lower than `list_presence`
#: allows, because each entry here carries a line per network rather than one.
MAX_LIMIT = 100

_SELF_REPORTED = (
    "Presence is self-reported by each network in PeeringDB; "
    "a shared location missing here is unrecorded, not absent."
)


async def find_common_presence(
    client: PeeringDBClient, asns: list[int], limit: int = DEFAULT_LIMIT
) -> ToolResult[CommonPresence]:
    """Find the exchanges and facilities all of `asns` are present at."""
    wanted = _distinct(asns)

    out_of_range = [asn for asn in asns if not _MIN_ASN <= asn <= _MAX_ASN]
    if out_of_range:
        listed = ", ".join(str(asn) for asn in out_of_range)
        return ToolResult(
            status=Status.INVALID_INPUT,
            note=f"{listed} is not a valid AS number; "
            f"give numbers between {_MIN_ASN} and {_MAX_ASN}.",
        )
    if not MIN_ASNS <= len(wanted) <= MAX_ASNS:
        return ToolResult(
            status=Status.INVALID_INPUT,
            note=f"Give between {MIN_ASNS} and {MAX_ASNS} different AS numbers; "
            f"got {len(wanted)}. Use list_presence for where one network is present.",
        )
    if limit < 1:
        return ToolResult(
            status=Status.INVALID_INPUT,
            note=f"limit must be at least 1; default {DEFAULT_LIMIT}, at most {MAX_LIMIT}.",
        )
    limit = min(limit, MAX_LIMIT)

    try:
        return await _find(client, wanted, limit)
    except UpstreamNotFoundError:
        return _not_found(wanted)
    except UpstreamError as exc:
        return upstream_failure(exc)


async def _find(client: PeeringDBClient, asns: list[int], limit: int) -> ToolResult[CommonPresence]:
    networks_fetch = await client.networks_by_asns(asns)
    by_asn = {network.asn: network for network in networks_fetch.value}

    missing = [asn for asn in asns if asn not in by_asn]
    if missing:
        return _not_found(missing)

    net_ids = [by_asn[asn].record_id for asn in asns]
    unkeyed = [asn for asn, net_id in zip(asns, net_ids, strict=True) if net_id is None]
    if unkeyed:
        # Cannot happen for a real record, but the id is optional at the
        # boundary and a facility query without one would fetch 14 MB.
        listed = ", ".join(f"AS{asn}" for asn in unkeyed)
        return ToolResult(
            status=Status.UPSTREAM_UNAVAILABLE,
            note=f"PeeringDB returned {listed} without the id its facility list is keyed on.",
        )
    keys = [net_id for net_id in net_ids if net_id is not None]

    ports = await client.exchange_presence_for_asns(asns)
    sites = await client.facility_presence_for_net_ids(keys)
    fetches: list[Fetched[object]] = [networks_fetch, ports, sites]

    exchanges_by_asn = group_exchanges_by_asn(ports.value)
    facilities_by_net = group_facilities_by_net_id(sites.value)
    totals = [
        PresenceTotals(
            asn=asn,
            name=by_asn[asn].name,
            exchanges=len(exchanges_by_asn.get(asn, [])),
            facilities=len(facilities_by_net.get(net_id, {})),
        )
        for asn, net_id in zip(asns, keys, strict=True)
    ]

    matches = shared_exchanges(exchanges_by_asn, asns)
    page = matches[:limit]
    detail = await client.exchanges_by_id(group.ix_id for group in page)
    fetches.append(detail)
    known = {record.record_id: record for record in detail.value}
    exchanges = Page[SharedExchange](
        items=[shape_shared_exchange(group, known.get(group.ix_id)) for group in page],
        total=len(matches),
        truncated=len(matches) > limit,
    )

    sites_shared = shared_facilities(facilities_by_net, keys)
    facilities = Page[FacilityPresence](
        items=[shape_facility_presence(row) for row in sites_shared[:limit]],
        total=len(sites_shared),
        truncated=len(sites_shared) > limit,
    )

    data = CommonPresence(networks=totals, exchanges=exchanges, facilities=facilities)
    provenance = Provenance.now(
        "peeringdb",
        record_updated=_latest_edit([*ports.value, *sites.value]),
        from_cache=all(fetch.from_cache for fetch in fetches),
    )

    blank = [total for total in totals if not total.exchanges and not total.facilities]
    if blank:
        return ToolResult(
            status=Status.NOT_RECORDED,
            data=data,
            note=_nothing_recorded(blank),
            provenance=provenance,
        )

    return ToolResult(
        status=Status.OK,
        data=data,
        note=_note(totals, exchanges, facilities, limit),
        provenance=provenance,
    )


def _distinct(asns: list[int]) -> list[int]:
    """The AS numbers asked for, once each, in the order asked.

    A caller repeating a network is asking a smaller question than it thinks:
    the overlap of AS3320 with itself is everywhere AS3320 is. De-duplicating
    turns that into a count the validation above can refuse.
    """
    seen: dict[int, None] = {}
    for asn in asns:
        seen.setdefault(asn, None)
    return list(seen)


def _note(
    totals: list[PresenceTotals],
    exchanges: Page[SharedExchange],
    facilities: Page[FacilityPresence],
    limit: int,
) -> str:
    """The caveat, and what was cut or missing. Two sentences at the outside."""
    if not exchanges.total and not facilities.total:
        listed = " and ".join(f"AS{total.asn}" for total in totals)
        return (
            f"{listed} record no exchange or facility in common. "
            "Each one's totals are in networks, so this is an absent overlap, "
            "not an absent record."
        )

    cuts = []
    if exchanges.truncated:
        cuts.append(f"{limit} of {exchanges.total} shared exchanges (widest bottleneck first)")
    if facilities.truncated:
        cuts.append(f"{limit} of {facilities.total} shared facilities (by country)")
    if not cuts:
        return _SELF_REPORTED
    return (
        f"{_SELF_REPORTED} Showing {' and '.join(cuts)}; raise limit for more, at most {MAX_LIMIT}."
    )


def _nothing_recorded(blank: list[PresenceTotals]) -> str:
    """Say which network has nothing recorded, so the empty overlap is explained."""
    listed = " and ".join(f"{total.name} (AS{total.asn})" for total in blank)
    verb = "records" if len(blank) == 1 else "record"
    return (
        f"{listed} {verb} no presence anywhere in PeeringDB, so no shared location "
        "can be worked out. Absence here is not evidence of absence in reality."
    )


def _not_found(asns: list[int]) -> ToolResult[CommonPresence]:
    listed = ", ".join(f"AS{asn}" for asn in asns)
    plural = "is" if len(asns) == 1 else "are"
    return ToolResult(
        status=Status.NOT_FOUND,
        note=f"{listed} {plural} not listed in PeeringDB, so there is nothing to "
        "intersect. Many networks route traffic without registering there.",
    )


def _latest_edit(rows: Iterable[UpstreamRecord]) -> datetime | None:
    """When any presence record was last touched: the age of the answer as a whole."""
    stamps = [row.updated for row in rows if row.updated is not None]
    return max(stamps) if stamps else None


__all__ = ["DEFAULT_LIMIT", "MAX_ASNS", "MAX_LIMIT", "MIN_ASNS", "find_common_presence"]
