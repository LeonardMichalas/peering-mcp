"""Unit tests for the presence half of the shaping layer.

Grouping, ordering and the two entry shapes. Pure functions, so the tests are
plain: real rows in, a compact list out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from peering_mcp.models.upstream import (
    UpstreamExchange,
    UpstreamNetworkFacility,
    UpstreamNetworkIxLan,
)
from peering_mcp.shaping import (
    ExchangeGroup,
    group_by_exchange,
    shape_exchange_presence,
    shape_facility_presence,
    sort_facilities,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "peeringdb"


def rows(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text())["data"]


def port(ix_id: int, **fields: Any) -> UpstreamNetworkIxLan:
    return UpstreamNetworkIxLan.model_validate({"asn": 1, "ix_id": ix_id, **fields})


# --- Grouping ports into exchanges -------------------------------------------


def test_one_group_per_exchange_not_per_port() -> None:
    """AS3320 records 8 ports at 7 exchanges. The caller should see 7."""
    ports = [UpstreamNetworkIxLan.model_validate(r) for r in rows("netixlan_as3320.json")]

    groups = group_by_exchange(ports)

    assert len(ports) == 8, "fixture drifted; re-record it"
    assert len(groups) == 7
    doubled = next(g for g in groups if len(g.ports) == 2)
    assert doubled.speed_mbps == sum(p.speed or 0 for p in doubled.ports)


def test_largest_capacity_comes_first() -> None:
    groups = group_by_exchange([port(1, speed=10), port(2, speed=1000), port(3, speed=100)])

    assert [g.ix_id for g in groups] == [2, 3, 1]


def test_ties_break_on_exchange_id_so_the_order_is_stable() -> None:
    groups = group_by_exchange([port(9, speed=10), port(3, speed=10), port(5, speed=10)])

    assert [g.ix_id for g in groups] == [3, 5, 9]


def test_a_port_without_a_speed_sorts_last_not_first() -> None:
    groups = group_by_exchange([port(1), port(2, speed=1)])

    assert [g.ix_id for g in groups] == [2, 1]


def test_speed_is_none_only_when_no_port_reports_one() -> None:
    assert ExchangeGroup(ix_id=1, ports=(port(1), port(1))).speed_mbps is None
    assert ExchangeGroup(ix_id=1, ports=(port(1), port(1, speed=10))).speed_mbps == 10


def test_route_server_is_any_port_with_nothing_said_staying_none() -> None:
    assert ExchangeGroup(ix_id=1, ports=(port(1), port(1))).route_server is None
    assert ExchangeGroup(ix_id=1, ports=(port(1, is_rs_peer=False),)).route_server is False
    assert (
        ExchangeGroup(ix_id=1, ports=(port(1, is_rs_peer=False), port(1, is_rs_peer=True)))
    ).route_server is True


# --- Shaping an exchange entry ----------------------------------------------


def test_location_comes_from_the_exchange_record() -> None:
    exchange = UpstreamExchange.model_validate(rows("ix_de_cix_frankfurt.json")[0])
    group = ExchangeGroup(ix_id=31, ports=(port(31, speed=100_000, is_rs_peer=True),))

    entry = shape_exchange_presence(group, exchange)

    assert entry.model_dump() == {
        "name": "DE-CIX Frankfurt",
        "city": "Frankfurt",
        "country": "DE",
        "speed_mbps": 100_000,
        "ports": 1,
        "route_server": True,
    }


def test_a_missing_exchange_record_degrades_rather_than_dropping_the_exchange() -> None:
    """The network really is there; not knowing where "there" is, is not a reason to hide it."""
    group = ExchangeGroup(ix_id=64, ports=(port(64, name="NL-ix: Main"),))

    entry = shape_exchange_presence(group, None)

    assert entry.name == "NL-ix: Main"
    assert entry.city is None
    assert entry.country is None


def test_with_no_name_anywhere_the_entry_still_identifies_the_exchange() -> None:
    entry = shape_exchange_presence(ExchangeGroup(ix_id=64, ports=(port(64),)), None)

    assert "64" in entry.name


# --- Facilities -------------------------------------------------------------


def test_a_facility_entry_carries_place_and_nothing_else() -> None:
    row = UpstreamNetworkFacility.model_validate(rows("netfac_net196.json")[0])

    entry = shape_facility_presence(row)

    assert entry.model_dump() == {
        "name": "Equinix DC1-DC15,DC21-DC22 - Ashburn",
        "city": "Ashburn",
        "country": "US",
    }


def site(fac_id: int, **fields: Any) -> UpstreamNetworkFacility:
    return UpstreamNetworkFacility.model_validate({"net_id": 1, "fac_id": fac_id, **fields})


def test_facilities_group_by_country_then_city_then_name() -> None:
    ordered = sort_facilities(
        [
            site(1, country="US", city="Ashburn", name="Equinix DC2"),
            site(2, country="DE", city="Frankfurt", name="Interxion FRA1"),
            site(3, country="US", city="Ashburn", name="Equinix DC1"),
            site(4, country="DE", city="Berlin", name="IPB"),
        ]
    )

    assert [s.fac_id for s in ordered] == [4, 2, 3, 1]


def test_an_unrecorded_place_sorts_last_not_first() -> None:
    ordered = sort_facilities(
        [site(1), site(2, country="AT", city="Vienna"), site(3, country="AT")]
    )

    assert [s.fac_id for s in ordered] == [2, 3, 1]


def test_facility_order_is_stable_on_the_large_fixture() -> None:
    sites = [UpstreamNetworkFacility.model_validate(r) for r in rows("netfac_net291.json")]

    once = [s.fac_id for s in sort_facilities(sites)]
    again = [s.fac_id for s in sort_facilities(reversed(sites))]

    assert once == again
