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

from collections.abc import Iterable
from dataclasses import dataclass

from peering_mcp.models.domain import (
    ExchangePresence,
    FacilityPresence,
    Network,
    NetworkMatch,
    PeeringPolicy,
)
from peering_mcp.models.upstream import (
    UpstreamExchange,
    UpstreamNetwork,
    UpstreamNetworkFacility,
    UpstreamNetworkIxLan,
)


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


# --- Presence ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExchangeGroup:
    """Every port one network records at one exchange, combined.

    PeeringDB records a row per port, so a network with two ports at DE-CIX
    Frankfurt has two rows there. A caller asking "where are they present"
    wants the exchange once, with the ports summed.
    """

    ix_id: int
    ports: tuple[UpstreamNetworkIxLan, ...]

    @property
    def speed_mbps(self) -> int | None:
        """Total capacity across ports, or None when no port reports a speed."""
        known = [port.speed for port in self.ports if port.speed is not None]
        return sum(known) if known else None

    @property
    def route_server(self) -> bool | None:
        """True if any port peers with the route server, None if nothing says."""
        flags = [port.is_rs_peer for port in self.ports if port.is_rs_peer is not None]
        if not flags:
            return None
        return any(flags)

    @property
    def fallback_name(self) -> str | None:
        """The name the port row carries, for when the exchange record is missing."""
        return next((port.name for port in self.ports if port.name), None)


def group_by_exchange(rows: Iterable[UpstreamNetworkIxLan]) -> list[ExchangeGroup]:
    """Fold port rows into one group per exchange, largest capacity first.

    Ordered by total speed so that when a long list is cut, the part that is
    kept is the part that matters. Ties break on the exchange id, so the order
    is stable across runs.
    """
    ports: dict[int, list[UpstreamNetworkIxLan]] = {}
    for row in rows:
        ports.setdefault(row.ix_id, []).append(row)
    groups = [ExchangeGroup(ix_id=ix_id, ports=tuple(rows)) for ix_id, rows in ports.items()]
    return sorted(groups, key=lambda group: (-(group.speed_mbps or 0), group.ix_id))


def shape_exchange_presence(
    group: ExchangeGroup, exchange: UpstreamExchange | None
) -> ExchangePresence:
    """One exchange entry: where it is and how much capacity the network has there.

    City and country live on the exchange record, not the port row, which is
    why the exchange is looked up separately. Without it the entry falls back
    to the name the port row carries and leaves the location unrecorded, rather
    than dropping an exchange the network really is at.
    """
    if exchange is not None:
        name = exchange.name
        city, country = exchange.city, exchange.country
    else:
        name = group.fallback_name or f"exchange {group.ix_id}"
        city = country = None
    return ExchangePresence(
        name=name,
        city=city,
        country=country,
        speed_mbps=group.speed_mbps,
        ports=len(group.ports),
        route_server=group.route_server,
    )


def shape_facility_presence(row: UpstreamNetworkFacility) -> FacilityPresence:
    """One facility entry. The `/netfac` row already carries name and place."""
    return FacilityPresence(name=row.name, city=row.city, country=row.country)


def sort_facilities(rows: Iterable[UpstreamNetworkFacility]) -> list[UpstreamNetworkFacility]:
    """Country, then city, then name, with anything unrecorded at the end.

    Facilities have no natural weight the way an exchange port has a speed, so
    the order groups them geographically, which is how the question is usually
    asked. Stable across runs: the facility id breaks every tie.
    """

    def key(row: UpstreamNetworkFacility) -> tuple[int, str, int, str, int, str, int]:
        return (
            row.country is None,
            row.country or "",
            row.city is None,
            row.city or "",
            row.name is None,
            row.name or "",
            row.fac_id,
        )

    return sorted(rows, key=key)
