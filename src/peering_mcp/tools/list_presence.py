"""The `list_presence` tool.

Where one network is present: which internet exchanges, which facilities.

The first tool that has to cut a list. One network's raw presence records run
to 130 KB, and a caller gets a page of them, largest first, with the total and
a flag saying the page is not the whole. The order and the cut are decided in
`shaping.py`; this module only says what was asked for and what came back.

Four upstream requests at most, in this order and for these reasons:

1. `/net` — whether the network exists at all. `/netixlan` answers 200 with
   an empty list for any ASN, listed or not, so without this step an unlisted
   network and a listed one with no presence would look the same.
2. `/netixlan` — every exchange port, one row each.
3. `/ix` — the exchanges on the page being returned, because the port rows do
   not say where an exchange is. Only the page, never the whole list.
4. `/netfac` — every facility, keyed by the network's PeeringDB id from step 1.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from peering_mcp.clients.http import Fetched
from peering_mcp.clients.peeringdb import PeeringDBClient
from peering_mcp.errors import UpstreamError, UpstreamNotFoundError
from peering_mcp.models.domain import (
    ExchangePresence,
    FacilityPresence,
    Page,
    PresenceList,
    Provenance,
    Status,
    ToolResult,
)
from peering_mcp.models.upstream import UpstreamNetwork, UpstreamRecord
from peering_mcp.shaping import (
    group_by_exchange,
    shape_exchange_presence,
    shape_facility_presence,
    sort_facilities,
)
from peering_mcp.tools._envelope import upstream_failure
from peering_mcp.tools.lookup_network import _MAX_ASN, _MIN_ASN

Kind = Literal["ix", "facility", "both"]

DEFAULT_LIMIT = 50
#: The most entries one page may carry. A caller who wants more than this is
#: better served by asking a narrower question than by a 40 KB answer.
MAX_LIMIT = 200

_SELF_REPORTED = (
    "Presence is self-reported by the network in PeeringDB; "
    "a location missing here is unrecorded, not absent."
)


async def list_presence(
    client: PeeringDBClient,
    asn: int,
    kind: Kind = "both",
    limit: int = DEFAULT_LIMIT,
) -> ToolResult[PresenceList]:
    """List the exchanges and facilities one network records a presence at."""
    if not _MIN_ASN <= asn <= _MAX_ASN:
        return ToolResult(
            status=Status.INVALID_INPUT,
            note=f"{asn} is not a valid AS number; give one between {_MIN_ASN} and {_MAX_ASN}.",
        )
    if limit < 1:
        return ToolResult(
            status=Status.INVALID_INPUT,
            note=f"limit must be at least 1; default {DEFAULT_LIMIT}, at most {MAX_LIMIT}.",
        )
    limit = min(limit, MAX_LIMIT)

    try:
        return await _list(client, asn, kind, limit)
    except UpstreamNotFoundError:
        return _not_found(asn)
    except UpstreamError as exc:
        return upstream_failure(exc)


async def _list(
    client: PeeringDBClient, asn: int, kind: Kind, limit: int
) -> ToolResult[PresenceList]:
    fetches: list[Fetched[object]] = []
    rows: list[UpstreamRecord] = []

    network_fetch = await client.network_by_asn(asn)
    fetches.append(network_fetch)
    network = network_fetch.value
    if network is None:
        return _not_found(asn)

    exchanges: Page[ExchangePresence] | None = None
    facilities: Page[FacilityPresence] | None = None
    cuts: list[str] = []

    if kind in ("ix", "both"):
        ports = await client.exchange_presence(asn)
        fetches.append(ports)
        rows.extend(ports.value)
        groups = group_by_exchange(ports.value)
        page = groups[:limit]
        detail = await client.exchanges_by_id(group.ix_id for group in page)
        fetches.append(detail)
        by_id = {record.record_id: record for record in detail.value}
        exchanges = Page(
            items=[shape_exchange_presence(group, by_id.get(group.ix_id)) for group in page],
            total=len(groups),
            truncated=len(groups) > limit,
        )
        if exchanges.truncated:
            cuts.append(f"{limit} of {exchanges.total} exchanges (largest ports first)")

    if kind in ("facility", "both"):
        if network.record_id is None:
            # Cannot happen for a real record, but the field is optional at the
            # boundary and a facility query without an id would fetch 14 MB.
            return ToolResult(
                status=Status.UPSTREAM_UNAVAILABLE,
                note=f"PeeringDB returned AS{asn} without the id its facility list is keyed on.",
            )
        sites = await client.facility_presence(network.record_id)
        fetches.append(sites)
        rows.extend(sites.value)
        ordered = sort_facilities(sites.value)
        facilities = Page(
            items=[shape_facility_presence(row) for row in ordered[:limit]],
            total=len(ordered),
            truncated=len(ordered) > limit,
        )
        if facilities.truncated:
            cuts.append(f"{limit} of {facilities.total} facilities (by country)")

    provenance = Provenance.now(
        "peeringdb",
        record_updated=_latest_edit(rows),
        from_cache=all(fetch.from_cache for fetch in fetches),
    )

    if not rows:
        return ToolResult(
            status=Status.NOT_RECORDED,
            note=_nothing_recorded(network, kind),
            provenance=provenance,
        )

    return ToolResult(
        status=Status.OK,
        data=PresenceList(
            asn=asn, network=network.name, exchanges=exchanges, facilities=facilities
        ),
        note=_SELF_REPORTED + _truncation_note(cuts),
        provenance=provenance,
    )


def _truncation_note(cuts: list[str]) -> str:
    """One sentence saying what was cut and how to get the rest, or nothing."""
    if not cuts:
        return ""
    return f" Showing {' and '.join(cuts)}; raise limit for more, at most {MAX_LIMIT}."


def _not_found(asn: int) -> ToolResult[PresenceList]:
    return ToolResult(
        status=Status.NOT_FOUND,
        note=f"AS{asn} is not listed in PeeringDB. "
        "Many networks route traffic without registering there.",
    )


def _nothing_recorded(network: UpstreamNetwork, kind: Kind) -> str:
    """Say what is missing, and what the network does record instead.

    The counts on the network record cost nothing and turn "no exchanges" into
    "no exchanges, but 53 facilities", which is the difference between a
    network that filled nothing in and one that is simply not at an exchange.
    """
    asked = {"ix": "exchange presence", "facility": "facility presence", "both": "presence"}[kind]
    note = f"{network.name} (AS{network.asn}) is listed in PeeringDB but records no {asked}."
    if kind == "ix" and network.fac_count:
        note += f" It does record {network.fac_count} facilities."
    elif kind == "facility" and network.ix_count:
        note += f" It does record {network.ix_count} exchanges."
    return note + " Absence here is not evidence of absence in reality."


def _latest_edit(rows: list[UpstreamRecord]) -> datetime | None:
    """When any presence record was last touched: the age of the list as a whole."""
    stamps = [row.updated for row in rows if row.updated is not None]
    return max(stamps) if stamps else None
