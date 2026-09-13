"""The `lookup_network` tool.

Resolves an AS number or a name fragment to a network, with its peering policy.
Deliberately shapes the upstream record down: PeeringDB returns 42 fields per
network, most of which are internal identifiers or free text with no decision
value, and passing them through would cost the caller context for nothing.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from peering_mcp.clients.peeringdb import NAME_MATCH_LIMIT, PeeringDBClient
from peering_mcp.errors import (
    UpstreamError,
    UpstreamNotFoundError,
    UpstreamRateLimitedError,
)
from peering_mcp.models.domain import (
    Network,
    NetworkLookup,
    NetworkMatch,
    PeeringPolicy,
    Provenance,
    Status,
    ToolResult,
)

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


def _text(record: dict[str, Any], key: str) -> str | None:
    """Read a string field, treating empty strings as absent.

    PeeringDB uses `""` and `null` interchangeably for "not filled in", and the
    difference is never meaningful.
    """
    value = record.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _integer(record: dict[str, Any], key: str) -> int | None:
    value = record.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _timestamp(record: dict[str, Any], key: str) -> datetime | None:
    raw = _text(record, key)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def shape_network(record: dict[str, Any]) -> Network:
    """Turn a PeeringDB network record into the compact form callers get.

    Dropped on purpose: `notes` and `aka` are free text written by the network
    itself with no decision value, and they are the obvious place to hide
    instructions aimed at whatever model reads this. Internal identifiers,
    logos and social media are dropped because nobody asks about them.
    """
    ratio_required = record.get("policy_ratio")
    return Network(
        asn=_integer(record, "asn") or 0,
        name=_text(record, "name") or "(unnamed)",
        long_name=_text(record, "name_long"),
        website=_text(record, "website"),
        network_type=_text(record, "info_type"),
        traffic_estimate=_text(record, "info_traffic"),
        scope=_text(record, "info_scope"),
        traffic_ratio=_text(record, "info_ratio"),
        ipv4_prefixes=_integer(record, "info_prefixes4"),
        ipv6_prefixes=_integer(record, "info_prefixes6"),
        exchange_count=_integer(record, "ix_count"),
        facility_count=_integer(record, "fac_count"),
        policy=PeeringPolicy(
            general=_text(record, "policy_general"),
            locations=_text(record, "policy_locations"),
            ratio_required=ratio_required if isinstance(ratio_required, bool) else None,
            contract_required=_text(record, "policy_contracts"),
            url=_text(record, "policy_url"),
        ),
        irr_as_set=_text(record, "irr_as_set"),
        looking_glass=_text(record, "looking_glass"),
    )


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
    except UpstreamRateLimitedError:
        return ToolResult(
            status=Status.RATE_LIMITED,
            note="PeeringDB is throttling requests. Wait a moment and try again.",
        )
    except UpstreamError as exc:
        return ToolResult(
            status=Status.UPSTREAM_UNAVAILABLE,
            note=f"Could not reach PeeringDB: {exc}",
        )


async def _by_asn(client: PeeringDBClient, asn: int) -> ToolResult[NetworkLookup]:
    record = await client.network_by_asn(asn)
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
        provenance=Provenance.now("peeringdb", record_updated=_timestamp(record, "updated")),
    )


async def _by_name(client: PeeringDBClient, fragment: str) -> ToolResult[NetworkLookup]:
    records = await client.networks_by_name(fragment)
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
            provenance=Provenance.now("peeringdb", record_updated=_timestamp(record, "updated")),
        )

    candidates = [
        NetworkMatch(asn=_integer(row, "asn") or 0, name=_text(row, "name") or "(unnamed)")
        for row in records[:NAME_MATCH_LIMIT]
    ]
    note = (
        f"{len(records)} networks match {fragment!r}. "
        "Pick one and call this tool again with its AS number."
    )
    if len(records) > NAME_MATCH_LIMIT:
        note += f" Showing the first {NAME_MATCH_LIMIT}."
    return ToolResult(
        status=Status.OK,
        data=NetworkLookup(candidates=candidates),
        note=note,
        provenance=Provenance.now("peeringdb"),
    )
