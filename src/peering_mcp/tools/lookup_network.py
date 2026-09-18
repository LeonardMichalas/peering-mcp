"""The `lookup_network` tool.

Resolves an AS number or a name fragment to a network, with its peering policy.

This module owns two things and nothing else: turning a caller's query into a
client call, and turning what comes back — including what goes wrong — into the
envelope. Validating the upstream response belongs to `clients/peeringdb.py`,
and deciding what is worth returning belongs to `shaping.py`.
"""

from __future__ import annotations

import re

from peering_mcp.clients.peeringdb import NAME_MATCH_LIMIT, PeeringDBClient
from peering_mcp.errors import UpstreamError, UpstreamNotFoundError
from peering_mcp.models.domain import (
    NetworkLookup,
    Provenance,
    Status,
    ToolResult,
)
from peering_mcp.shaping import shape_network, shape_network_match
from peering_mcp.tools._envelope import upstream_failure

#: The valid 32-bit AS number range. 0 and 4294967295 are reserved.
_MIN_ASN = 1
_MAX_ASN = 4_294_967_294

_ASN_PATTERN = re.compile(r"^\s*(?:as)?(\d+)\s*$", re.IGNORECASE)

_SELF_REPORTED = (
    "PeeringDB records are maintained by the networks themselves. "
    "Treat a missing field as unrecorded, not as evidence it is untrue."
)


def parse_asn(query: str) -> int | None:
    """Return the AS number in `query`, or None if it is not one.

    Accepts `AS3320`, `as3320`, `3320` and surrounding whitespace.
    """
    match = _ASN_PATTERN.match(query)
    if match is None:
        return None
    value = int(match.group(1))
    if not _MIN_ASN <= value <= _MAX_ASN:
        return None
    return value


async def lookup_network(client: PeeringDBClient, query: str) -> ToolResult[NetworkLookup]:
    """Look up one network by AS number or name."""
    cleaned = query.strip()
    if not cleaned:
        return ToolResult(
            status=Status.INVALID_INPUT,
            note="Give an AS number such as AS3320, or part of a network's name.",
        )

    asn = parse_asn(cleaned)
    try:
        if asn is not None:
            return await _by_asn(client, asn)
        return await _by_name(client, cleaned)
    except UpstreamNotFoundError:
        return ToolResult(
            status=Status.NOT_FOUND,
            note=f"PeeringDB has no network matching {cleaned!r}. "
            "A network can exist and route traffic without being listed here.",
        )
    except UpstreamError as exc:
        return upstream_failure(exc)


async def _by_asn(client: PeeringDBClient, asn: int) -> ToolResult[NetworkLookup]:
    fetched = await client.network_by_asn(asn)
    record = fetched.value
    if record is None:
        return ToolResult(
            status=Status.NOT_FOUND,
            note=f"AS{asn} is not listed in PeeringDB. "
            "Many networks route traffic without registering there.",
        )
    return ToolResult(
        status=Status.OK,
        data=NetworkLookup(network=shape_network(record)),
        note=_SELF_REPORTED,
        provenance=Provenance.now(
            "peeringdb", record_updated=record.updated, from_cache=fetched.from_cache
        ),
    )


async def _by_name(client: PeeringDBClient, fragment: str) -> ToolResult[NetworkLookup]:
    fetched = await client.networks_by_name(fragment)
    records = fetched.value
    if not records:
        return ToolResult(
            status=Status.NOT_FOUND,
            note=f"No network in PeeringDB has {fragment!r} in its name. "
            "Try a shorter fragment, or look the AS number up directly.",
        )

    if len(records) == 1:
        record = records[0]
        return ToolResult(
            status=Status.OK,
            data=NetworkLookup(network=shape_network(record)),
            note=_SELF_REPORTED,
            provenance=Provenance.now(
                "peeringdb", record_updated=record.updated, from_cache=fetched.from_cache
            ),
        )

    candidates = [shape_network_match(row) for row in records[:NAME_MATCH_LIMIT]]
    note = (
        f"{len(records)} networks match {fragment!r}. "
        "Pick one and call this tool again with its AS number."
    )
    if len(records) > NAME_MATCH_LIMIT:
        note += f" Showing the first {NAME_MATCH_LIMIT}."
    return ToolResult(
        status=Status.AMBIGUOUS,
        data=NetworkLookup(candidates=candidates),
        note=note,
        provenance=Provenance.now("peeringdb", from_cache=fetched.from_cache),
    )
