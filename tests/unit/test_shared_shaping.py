"""Unit tests for the intersection half of the shaping layer.

Splitting one batched response per network, intersecting, and the ordering that
decides what survives a cut. Pure functions, so the tests are small rows in and
a short list out.
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
    group_exchanges_by_asn,
    group_facilities_by_net_id,
    shape_shared_exchange,
    shared_exchanges,
    shared_facilities,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "peeringdb"


def rows(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text())["data"]


def port(asn: int, ix_id: int, **fields: Any) -> UpstreamNetworkIxLan:
    return UpstreamNetworkIxLan.model_validate({"asn": asn, "ix_id": ix_id, **fields})


def site(net_id: int, fac_id: int, **fields: Any) -> UpstreamNetworkFacility:
    return UpstreamNetworkFacility.model_validate({"net_id": net_id, "fac_id": fac_id, **fields})


def shared(ports: list[UpstreamNetworkIxLan], asns: list[int]) -> list[int]:
    return [group.ix_id for group in shared_exchanges(group_exchanges_by_asn(ports), asns)]


# --- Splitting one response per network ---------------------------------------


def test_a_batched_response_splits_into_one_list_per_network() -> None:
    by_asn = group_exchanges_by_asn(
        [UpstreamNetworkIxLan.model_validate(r) for r in rows("netixlan_as3320_6695_6939.json")]
    )

    assert {asn: len(groups) for asn, groups in by_asn.items()} == {3320: 7, 6695: 1, 6939: 335}


def test_each_network_keeps_its_own_port_grouping() -> None:
    """AS3320 records 8 ports at 7 exchanges even inside a three-network response."""
    by_asn = group_exchanges_by_asn(
        [UpstreamNetworkIxLan.model_validate(r) for r in rows("netixlan_as3320_6695_6939.json")]
    )

    doubled = [group for group in by_asn[3320] if len(group.ports) == 2]
    assert len(doubled) == 1
    assert doubled[0].asn == 3320


def test_facilities_split_by_network_id_and_key_on_the_facility() -> None:
    by_net = group_facilities_by_net_id(
        [UpstreamNetworkFacility.model_validate(r) for r in rows("netfac_net196_291_947.json")]
    )

    assert {net_id: len(sites) for net_id, sites in by_net.items()} == {
        196: 53,
        291: 343,
        947: 10,
    }


def test_the_same_facility_recorded_twice_is_one_place() -> None:
    by_net = group_facilities_by_net_id([site(1, 5, name="A"), site(1, 5, name="A")])

    assert list(by_net[1]) == [5]


# --- The intersection ----------------------------------------------------------


def test_only_exchanges_every_network_is_at_survive() -> None:
    ports = [port(1, 10), port(1, 20), port(2, 20), port(2, 30), port(3, 20), port(3, 40)]

    assert shared(ports, [1, 2, 3]) == [20]


def test_two_of_the_three_would_have_shared_more() -> None:
    """The intersection is over all of them, never a majority."""
    ports = [port(1, 10), port(1, 20), port(2, 10), port(2, 20), port(3, 20)]

    assert shared(ports, [1, 2]) == [10, 20], "no speeds anywhere, so the id orders them"
    assert shared(ports, [1, 2, 3]) == [20]


def test_a_network_with_no_rows_shares_nothing() -> None:
    """Correct, and deliberately unexplained here: saying why is the tool's job."""
    ports = [port(1, 10), port(2, 10)]

    assert shared(ports, [1, 2, 3]) == []


def test_asking_about_no_networks_at_all_is_empty_rather_than_everything() -> None:
    assert shared([port(1, 10)], []) == []
    assert shared_facilities(group_facilities_by_net_id([site(1, 5)]), []) == []


def test_shared_facilities_are_the_facilities_all_of_them_record() -> None:
    sites = [site(1, 5), site(1, 6), site(2, 5), site(2, 7), site(3, 5), site(3, 6)]

    found = shared_facilities(group_facilities_by_net_id(sites), [1, 2, 3])

    assert [row.fac_id for row in found] == [5]


def test_a_shared_facility_is_described_by_the_first_network_asked_about() -> None:
    """Every row describes the same building; which one represents it must be fixed."""
    sites = [site(1, 5, name="Equinix FR5", city="Frankfurt"), site(2, 5, name="Equinix FR5")]

    found = shared_facilities(group_facilities_by_net_id(sites), [1, 2])

    assert found[0].city == "Frankfurt"


# --- The ordering that decides what survives a cut -------------------------------


def test_the_smaller_side_decides_the_order_not_the_sum() -> None:
    """A 10G port beside a 1T port is a 10G meeting point, and sorts like one."""
    ports = [
        port(1, 10, speed=1_000_000),
        port(2, 10, speed=10_000),
        port(1, 20, speed=100_000),
        port(2, 20, speed=100_000),
    ]

    assert shared(ports, [1, 2]) == [20, 10]


def test_an_unreported_speed_sorts_last_rather_than_first() -> None:
    """An unknown side is not a large one."""
    ports = [port(1, 10), port(2, 10), port(1, 20, speed=1), port(2, 20, speed=1)]

    assert shared(ports, [1, 2]) == [20, 10]


def test_ties_break_on_exchange_id_so_the_order_is_stable() -> None:
    ports = [port(a, ix, speed=100) for ix in (9, 3, 5) for a in (1, 2)]

    assert shared(ports, [1, 2]) == [3, 5, 9]


def test_the_order_asked_is_the_order_returned() -> None:
    """A position in `networks` identifies a network, so it cannot drift."""
    groups = shared_exchanges(
        group_exchanges_by_asn([port(1, 10), port(2, 10), port(3, 10)]), [3, 1, 2]
    )

    assert [group.asn for group in groups[0].per_network] == [3, 1, 2]


# --- Shaping one shared exchange --------------------------------------------------


def test_a_shared_entry_carries_every_network_and_the_place() -> None:
    groups = shared_exchanges(
        group_exchanges_by_asn(
            [port(1, 31, speed=100, is_rs_peer=True), port(2, 31, speed=200), port(2, 31)]
        ),
        [1, 2],
    )
    record = UpstreamExchange.model_validate(rows("ix_de_cix_frankfurt.json")[0])

    entry = shape_shared_exchange(groups[0], record)

    assert entry.name == "DE-CIX Frankfurt"
    assert entry.city == "Frankfurt"
    assert [member.asn for member in entry.networks] == [1, 2]
    assert entry.networks[0].route_server is True
    assert entry.networks[1].ports == 2
    assert entry.networks[1].speed_mbps == 200


def test_a_missing_exchange_record_degrades_rather_than_dropping_the_exchange() -> None:
    """They really do meet there. A failed lookup must not delete the answer."""
    groups = shared_exchanges(
        group_exchanges_by_asn([port(1, 64, name="NL-ix"), port(2, 64)]), [1, 2]
    )

    entry = shape_shared_exchange(groups[0], None)

    assert entry.name == "NL-ix"
    assert entry.city is None


def test_with_no_name_anywhere_the_entry_still_identifies_the_exchange() -> None:
    groups = shared_exchanges(group_exchanges_by_asn([port(1, 64), port(2, 64)]), [1, 2])

    entry = shape_shared_exchange(groups[0], None)

    assert "64" in entry.name
