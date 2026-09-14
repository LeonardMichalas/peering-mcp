"""Upstream record to the compact thing a caller gets back.

Pure functions, no I/O, no exceptions. Everything reaching here has already
been validated by `models/upstream.py`, so a shaping function's only job is to
decide what is worth a caller's context window.

**The shaping layer is the product.** A PeeringDB network record carries 42
fields. Most are internal identifiers, logos, social media links or free text
that no interconnection decision turns on, and passing them through would spend
a model's attention on noise. What comes out is under half that, and fits in
two kilobytes.

What is dropped is dropped at the boundary above, not here: fields this layer
never sees cannot be forgotten about. See `models/upstream.py`.
"""

from __future__ import annotations

from peering_mcp.models.domain import Network, NetworkMatch, PeeringPolicy
from peering_mcp.models.upstream import UpstreamNetwork


def shape_network(record: UpstreamNetwork) -> Network:
    """Turn a validated PeeringDB network record into the compact form."""
    return Network(
        asn=record.asn,
        name=record.name,
        long_name=record.name_long,
        website=record.website,
        network_type=record.info_type,
        traffic_estimate=record.info_traffic,
        scope=record.info_scope,
        traffic_ratio=record.info_ratio,
        ipv4_prefixes=record.info_prefixes4,
        ipv6_prefixes=record.info_prefixes6,
        exchange_count=record.ix_count,
        facility_count=record.fac_count,
        policy=PeeringPolicy(
            general=record.policy_general,
            locations=record.policy_locations,
            ratio_required=record.policy_ratio,
            contract_required=record.policy_contracts,
            url=record.policy_url,
        ),
        irr_as_set=record.irr_as_set,
        looking_glass=record.looking_glass,
    )


def shape_network_match(record: UpstreamNetwork) -> NetworkMatch:
    """A candidate in an ambiguous name search: enough to choose, nothing more."""
    return NetworkMatch(asn=record.asn, name=record.name)
