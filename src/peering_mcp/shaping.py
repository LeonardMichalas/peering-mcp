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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from peering_mcp.models.domain import (
    ExchangePresence,
    FacilityPresence,
    Network,
    NetworkMatch,
    ParticipantPorts,
    PeeringPolicy,
    SharedExchange,
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
    def asn(self) -> int:
        """Whose ports these are. Every row in a group belongs to one network."""
        return self.ports[0].asn

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


# --- Shared presence --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SharedExchangeGroup:
    """One exchange, with every requested network's ports at it.

    `per_network` runs in the order the caller asked, so an entry's position
    is its network. Nothing is missing by construction: a group only exists
    when every requested network has ports there.
    """

    ix_id: int
    per_network: tuple[ExchangeGroup, ...]

    @property
    def bottleneck_mbps(self) -> int | None:
        """The smallest capacity any one of them has here.

        Not the sum. A shared connection is limited by the smaller side, so an
        exchange where one network has a 10G port is a 10G meeting point
        however many terabits the other brings. `None` when any network has
        not said, because an unknown side is not a large one.
        """
        speeds: list[int] = []
        for group in self.per_network:
            if group.speed_mbps is None:
                return None
            speeds.append(group.speed_mbps)
        return min(speeds) if speeds else None

    @property
    def fallback_name(self) -> str | None:
        """The name any port row carries, for when the exchange record is missing."""
        names = (group.fallback_name for group in self.per_network)
        return next((name for name in names if name), None)


def group_exchanges_by_asn(
    rows: Iterable[UpstreamNetworkIxLan],
) -> dict[int, list[ExchangeGroup]]:
    """Split one multi-network `/netixlan` response into a list per network.

    A batched query answers for several networks at once, and every later step
    — the intersection and the per-network totals — is per network.
    """
    by_asn: dict[int, list[UpstreamNetworkIxLan]] = {}
    for row in rows:
        by_asn.setdefault(row.asn, []).append(row)
    return {asn: group_by_exchange(ports) for asn, ports in by_asn.items()}


def shared_exchanges(
    by_asn: Mapping[int, list[ExchangeGroup]], asns: Sequence[int]
) -> list[SharedExchangeGroup]:
    """Exchanges every one of `asns` is present at, widest bottleneck first.

    A network with no rows at all is an empty mapping entry, which makes the
    intersection empty — correctly, since nothing can be shared with a network
    that records nothing. Saying *why* it is empty is the tool's job, not this
    one's.
    """
    if not asns:
        return []
    indexed = [{group.ix_id: group for group in by_asn.get(asn, [])} for asn in asns]
    shared = set(indexed[0]).intersection(*indexed[1:])
    groups = [
        SharedExchangeGroup(ix_id=ix_id, per_network=tuple(per[ix_id] for per in indexed))
        for ix_id in shared
    ]
    return sorted(groups, key=lambda group: (-(group.bottleneck_mbps or 0), group.ix_id))


def shape_shared_exchange(
    group: SharedExchangeGroup, exchange: UpstreamExchange | None
) -> SharedExchange:
    """One shared exchange: where it is, and what each network has there.

    Degrades the same way `shape_exchange_presence` does. An exchange they
    really do share is worth returning without its city, and is never worth
    dropping because one lookup came back short.
    """
    if exchange is not None:
        name = exchange.name
        city, country = exchange.city, exchange.country
    else:
        name = group.fallback_name or f"exchange {group.ix_id}"
        city = country = None
    return SharedExchange(
        name=name,
        city=city,
        country=country,
        networks=[
            ParticipantPorts(
                asn=member.asn,
                speed_mbps=member.speed_mbps,
                ports=len(member.ports),
                route_server=member.route_server,
            )
            for member in group.per_network
        ],
    )


def group_facilities_by_net_id(
    rows: Iterable[UpstreamNetworkFacility],
) -> dict[int, dict[int, UpstreamNetworkFacility]]:
    """Split one multi-network `/netfac` response, keyed by network then facility.

    Keyed rather than listed because the facility id is what the intersection
    runs on, and because it collapses a network that somehow records the same
    facility twice into the one place it is.
    """
    by_net: dict[int, dict[int, UpstreamNetworkFacility]] = {}
    for row in rows:
        by_net.setdefault(row.net_id, {})[row.fac_id] = row
    return by_net


def shared_facilities(
    by_net_id: Mapping[int, Mapping[int, UpstreamNetworkFacility]], net_ids: Sequence[int]
) -> list[UpstreamNetworkFacility]:
    """Facilities every one of `net_ids` records a presence in.

    Every network's row describes the same building, so the first requested
    network's row represents it. That is a choice, not an accident: taking
    whichever row arrived first would reorder the output when upstream did.
    """
    if not net_ids:
        return []
    indexed = [by_net_id.get(net_id, {}) for net_id in net_ids]
    shared = set(indexed[0]).intersection(*indexed[1:])
    return sort_facilities(indexed[0][fac_id] for fac_id in shared)
